#!/usr/bin/env python3
"""Configurable DRAM-Bender RowHammer characterization runner.

The runner expands a JSON configuration into target/pattern/hammer-count points
and invokes the native collector once per point. It is intended for controlled
DRAM characterization, not software exploitation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence


class ConfigError(ValueError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return text.strip("_") or "UNNAMED"


def format_float_for_path(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace("-", "m").replace(".", "p")


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Top-level configuration must be a JSON object")
    return data


def require_dict(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be an object")
    return value


def require_list(parent: Dict[str, Any], key: str) -> List[Any]:
    value = parent.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"'{key}' must be a non-empty array")
    return value


def require_str(parent: Dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{key}' must be a non-empty string")
    return value.strip()


def get_str(parent: Dict[str, Any], key: str, default: str = "") -> str:
    value = parent.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    return value.strip()


def require_int(parent: Dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be an integer")
    return value


def get_int(parent: Dict[str, Any], key: str, default: int) -> int:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be an integer")
    return value


def require_number(parent: Dict[str, Any], key: str) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{key}' must be a number")
    return float(value)


def get_number(parent: Dict[str, Any], key: str, default: float) -> float:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{key}' must be a number")
    return float(value)


def get_bool(parent: Dict[str, Any], key: str, default: bool) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be true or false")
    return value


def normalize_hex_byte(value: Any, label: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        numeric = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            numeric = int(text, 16)
        except ValueError as exc:
            raise ConfigError(f"{label} must be a byte written as 00..FF") from exc
    else:
        raise ConfigError(f"{label} must be a byte written as 00..FF")
    if numeric < 0 or numeric > 255:
        raise ConfigError(f"{label} must be in 00..FF")
    return f"{numeric:02X}"


def validate_row_list(value: Any, label: str) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty integer array")
    result: List[int] = []
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ConfigError(f"{label} must contain only non-negative integers")
        if item in seen:
            raise ConfigError(f"{label} contains duplicate row {item}")
        seen.add(item)
        result.append(item)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temp, path)


def append_log(path: Path, message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def validate_and_normalize(config: Dict[str, Any], config_path: Path) -> Dict[str, Any]:
    run = require_dict(config, "run")
    board = require_dict(config, "board")
    dimm = require_dict(config, "dimm")
    environment = require_dict(config, "environment")
    geometry = require_dict(config, "geometry")
    timing = require_dict(config, "timing")
    experiment = require_dict(config, "experiment")
    raw_cases = require_list(config, "pattern_cases")
    raw_targets = require_list(config, "targets")

    require_str(run, "run_id")
    require_str(dimm, "dimm_id")
    require_number(environment, "temperature_c")
    require_number(environment, "voltage_vdd_v")

    banks_per_group = get_int(dimm, "banks_per_group", 4)
    if banks_per_group <= 0:
        raise ConfigError("dimm.banks_per_group must be positive")

    cache_lines = get_int(geometry, "cache_lines_per_row", 128)
    cache_line_bytes = get_int(geometry, "cache_line_bytes", 64)
    column_stride = get_int(geometry, "column_stride", 8)
    if cache_lines <= 0 or cache_line_bytes != 64 or column_stride <= 0:
        raise ConfigError("geometry must use positive cache_lines_per_row, cache_line_bytes=64, and positive column_stride")

    for key, default in (
        ("nominal_trcd_slots", 9),
        ("nominal_trp_slots", 9),
        ("write_spacing_slots", 7),
        ("read_spacing_slots", 7),
        ("write_recovery_slots", 8),
        ("read_to_precharge_slots", 8),
        ("final_guard_slots", 16),
        ("hammer_act_to_pre_cycles", 5),
        ("hammer_pre_to_act_cycles", 3),
        ("hammer_final_guard_cycles", 3),
    ):
        if get_int(timing, key, default) < 0:
            raise ConfigError(f"timing.{key} cannot be negative")
    if get_number(timing, "slot_ns_metadata", 1.5) <= 0:
        raise ConfigError("timing.slot_ns_metadata must be positive")
    if get_number(timing, "fabric_cycle_ns_metadata", 6.0) <= 0:
        raise ConfigError("timing.fabric_cycle_ns_metadata must be positive")

    repetitions = get_int(experiment, "repetitions", 10)
    if repetitions <= 0:
        raise ConfigError("experiment.repetitions must be positive")
    timeout = get_int(experiment, "timeout_seconds_per_point", 3600)
    retries = get_int(experiment, "max_retries", 0)
    if timeout <= 0 or retries < 0:
        raise ConfigError("experiment timeout must be positive and max_retries non-negative")

    hammer_counts_raw = require_list(experiment, "hammer_counts_per_row")
    hammer_counts: List[int] = []
    for value in hammer_counts_raw:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 0xFFFFFFFF:
            raise ConfigError("experiment.hammer_counts_per_row must contain positive 32-bit integers")
        hammer_counts.append(value)

    cases: List[Dict[str, Any]] = []
    seen_case_ids = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise ConfigError(f"pattern_cases[{index}] must be an object")
        case_id = require_str(raw_case, "case_id")
        if case_id in seen_case_ids:
            raise ConfigError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)
        cases.append(
            {
                "case_id": case_id,
                "victim_pattern": normalize_hex_byte(raw_case.get("victim_pattern"), f"pattern_cases[{index}].victim_pattern"),
                "aggressor_pattern": normalize_hex_byte(raw_case.get("aggressor_pattern"), f"pattern_cases[{index}].aggressor_pattern"),
            }
        )

    targets: List[Dict[str, Any]] = []
    seen_target_ids = set()
    for index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise ConfigError(f"targets[{index}] must be an object")
        target_id = require_str(raw_target, "target_id")
        if target_id in seen_target_ids:
            raise ConfigError(f"duplicate target_id: {target_id}")
        seen_target_ids.add(target_id)
        rank = require_int(raw_target, "rank")
        bank_group = require_int(raw_target, "bank_group")
        bank = require_int(raw_target, "bank")
        if min(rank, bank_group, bank) < 0:
            raise ConfigError(f"negative coordinates in target {target_id}")
        flat_bank = bank_group * banks_per_group + bank
        if flat_bank > 15:
            raise ConfigError(
                f"target {target_id} encodes flat bank {flat_bank}, outside 0..15; verify DIMM/bitstream geometry"
            )
        victim_rows = validate_row_list(raw_target.get("victim_rows"), f"targets[{index}].victim_rows")
        aggressor_rows = validate_row_list(raw_target.get("aggressor_rows"), f"targets[{index}].aggressor_rows")
        overlap = sorted(set(victim_rows) & set(aggressor_rows))
        if overlap:
            raise ConfigError(f"target {target_id} uses rows as both victim and aggressor: {overlap}")
        if len(aggressor_rows) > 128:
            raise ConfigError(f"target {target_id} has more than 128 aggressor rows")
        targets.append(
            {
                "target_id": target_id,
                "rank": rank,
                "bank_group": bank_group,
                "bank": bank,
                "victim_rows": victim_rows,
                "aggressor_rows": aggressor_rows,
                "notes": get_str(raw_target, "notes", ""),
            }
        )

    collector_binary = Path(config.get("collector_binary", "./rowhammer_collector"))
    output_root = Path(config.get("output_root", "./dram_rowhammer_data"))
    if not collector_binary.is_absolute():
        collector_binary = (config_path.parent / collector_binary).resolve()
    if not output_root.is_absolute():
        output_root = (config_path.parent / output_root).resolve()

    normalized = dict(config)
    normalized["collector_binary"] = str(collector_binary)
    normalized["output_root"] = str(output_root)
    normalized["pattern_cases"] = cases
    normalized["targets"] = targets
    normalized["experiment"] = dict(experiment)
    normalized["experiment"]["hammer_counts_per_row"] = hammer_counts
    return normalized


def make_run_root(config: Dict[str, Any]) -> Path:
    run = require_dict(config, "run")
    dimm = require_dict(config, "dimm")
    env = require_dict(config, "environment")
    temp = format_float_for_path(require_number(env, "temperature_c"), 2)
    voltage = format_float_for_path(require_number(env, "voltage_vdd_v"), 3)
    return (
        Path(require_str(config, "output_root"))
        / f"DIMM_{sanitize(require_str(dimm, 'dimm_id'))}"
        / f"RUN_{sanitize(require_str(run, 'run_id'))}__TEMP_{temp}C__VDD_{voltage}V"
    )


def target_dir_name(target: Dict[str, Any]) -> str:
    return (
        f"TARGET_{sanitize(target['target_id'])}"
        f"__R{target['rank']}__BG{target['bank_group']}__B{target['bank']}"
    )


def case_dir_name(case: Dict[str, Any]) -> str:
    return (
        f"CASE_{sanitize(case['case_id'])}"
        f"__V{case['victim_pattern']}__A{case['aggressor_pattern']}"
    )


def count_dir_name(count: int) -> str:
    return f"HAMMER_{count:010d}_PER_ROW"


def point_complete(point_dir: Path, repetitions: int) -> bool:
    return all((point_dir / f"REP_{rep:02d}" / ".complete").exists() for rep in range(repetitions))


def archive_incomplete_repetitions(point_dir: Path, repetitions: int, log_path: Path) -> None:
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_root = point_dir / "partial_attempts"
    for rep in range(repetitions):
        rep_dir = point_dir / f"REP_{rep:02d}"
        if not rep_dir.exists() or (rep_dir / ".complete").exists():
            continue
        if not any(rep_dir.iterdir()):
            shutil.rmtree(rep_dir)
            continue
        archive_root.mkdir(parents=True, exist_ok=True)
        destination = archive_root / f"REP_{rep:02d}__{stamp}"
        counter = 1
        while destination.exists():
            destination = archive_root / f"REP_{rep:02d}__{stamp}_{counter}"
            counter += 1
        shutil.move(str(rep_dir), str(destination))
        append_log(log_path, f"Archived incomplete {rep_dir.name} to {destination}")


def command_for_point(
    config: Dict[str, Any],
    point_dir: Path,
    target: Dict[str, Any],
    case: Dict[str, Any],
    hammer_count: int,
) -> List[str]:
    run = require_dict(config, "run")
    board = require_dict(config, "board")
    dimm = require_dict(config, "dimm")
    environment = require_dict(config, "environment")
    geometry = require_dict(config, "geometry")
    timing = require_dict(config, "timing")
    experiment = require_dict(config, "experiment")

    return [
        require_str(config, "collector_binary"),
        "--output-dir", str(point_dir),
        "--dimm-id", require_str(dimm, "dimm_id"),
        "--run-id", require_str(run, "run_id"),
        "--target-id", target["target_id"],
        "--case-id", case["case_id"],
        "--rank", str(target["rank"]),
        "--bank-group", str(target["bank_group"]),
        "--bank", str(target["bank"]),
        "--banks-per-group", str(get_int(dimm, "banks_per_group", 4)),
        "--victim-rows", ",".join(str(v) for v in target["victim_rows"]),
        "--aggressor-rows", ",".join(str(v) for v in target["aggressor_rows"]),
        "--victim-pattern", str(int(case["victim_pattern"], 16)),
        "--aggressor-pattern", str(int(case["aggressor_pattern"], 16)),
        "--hammer-count-per-row", str(hammer_count),
        "--cache-lines-per-row", str(get_int(geometry, "cache_lines_per_row", 128)),
        "--cache-line-bytes", str(get_int(geometry, "cache_line_bytes", 64)),
        "--column-stride", str(get_int(geometry, "column_stride", 8)),
        "--repetitions", str(get_int(experiment, "repetitions", 10)),
        "--nominal-trcd-slots", str(get_int(timing, "nominal_trcd_slots", 9)),
        "--nominal-trp-slots", str(get_int(timing, "nominal_trp_slots", 9)),
        "--write-spacing-slots", str(get_int(timing, "write_spacing_slots", 7)),
        "--read-spacing-slots", str(get_int(timing, "read_spacing_slots", 7)),
        "--write-recovery-slots", str(get_int(timing, "write_recovery_slots", 8)),
        "--read-to-precharge-slots", str(get_int(timing, "read_to_precharge_slots", 8)),
        "--final-guard-slots", str(get_int(timing, "final_guard_slots", 16)),
        "--hammer-act-to-pre-cycles", str(get_int(timing, "hammer_act_to_pre_cycles", 5)),
        "--hammer-pre-to-act-cycles", str(get_int(timing, "hammer_pre_to_act_cycles", 3)),
        "--hammer-final-guard-cycles", str(get_int(timing, "hammer_final_guard_cycles", 3)),
        "--slot-ns", str(get_number(timing, "slot_ns_metadata", 1.5)),
        "--fabric-cycle-ns", str(get_number(timing, "fabric_cycle_ns_metadata", 6.0)),
        "--temperature-c", str(require_number(environment, "temperature_c")),
        "--temperature-source", get_str(environment, "temperature_source", "manual"),
        "--voltage-vdd-v", str(require_number(environment, "voltage_vdd_v")),
        "--voltage-source", get_str(environment, "voltage_source", "manual"),
        "--refresh-enabled", str(get_bool(experiment, "refresh_enabled", True)).lower(),
        "--verify-before-hammer", str(get_bool(experiment, "verify_before_hammer", True)).lower(),
        "--write-observations-csv", str(get_bool(experiment, "write_observations_csv", False)).lower(),
        "--operator", get_str(run, "operator", ""),
        "--institution", get_str(run, "institution", ""),
        "--board-id", get_str(board, "board_id", "ALVEO_U200_01"),
        "--bitstream-file", get_str(board, "bitstream_file", "unknown"),
    ]


def run_command(
    command: Sequence[str],
    point_dir: Path,
    log_path: Path,
    timeout_seconds: int,
    max_retries: int,
) -> bool:
    status_path = point_dir / "point_status.json"
    attempt = 0
    while True:
        attempt += 1
        status = {
            "status": "running",
            "attempt": attempt,
            "started_at_utc": utc_now(),
            "command": list(command),
        }
        atomic_write_json(status_path, status)
        append_log(log_path, f"Executing attempt {attempt}: {' '.join(command)}")

        stdout_path = point_dir / f"collector_attempt_{attempt:02d}.stdout.log"
        stderr_path = point_dir / f"collector_attempt_{attempt:02d}.stderr.log"
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                completed = subprocess.run(
                    list(command),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    timeout=timeout_seconds,
                    check=False,
                    text=True,
                )
            if completed.returncode == 0:
                status.update(
                    {
                        "status": "complete",
                        "finished_at_utc": utc_now(),
                        "return_code": completed.returncode,
                    }
                )
                atomic_write_json(status_path, status)
                append_log(log_path, f"Point completed: {point_dir}")
                return True

            status.update(
                {
                    "status": "failed",
                    "finished_at_utc": utc_now(),
                    "return_code": completed.returncode,
                }
            )
            atomic_write_json(status_path, status)
            append_log(log_path, f"Collector failed with code {completed.returncode}: {point_dir}")
        except subprocess.TimeoutExpired:
            status.update(
                {
                    "status": "timeout",
                    "finished_at_utc": utc_now(),
                    "timeout_seconds": timeout_seconds,
                }
            )
            atomic_write_json(status_path, status)
            append_log(log_path, f"Collector timed out after {timeout_seconds}s: {point_dir}")

        if attempt > max_retries:
            return False
        append_log(log_path, f"Retrying point ({attempt}/{max_retries + 1})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run configurable DRAM-Bender RowHammer characterization")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="JSON configuration file")
    parser.add_argument("--dry-run", action="store_true", help="Create manifests and print commands without hardware")
    parser.add_argument("--force", action="store_true", help="Rerun completed points; existing repetition data is archived")
    parser.add_argument("--target", action="append", default=[], help="Run only the named target_id; may be repeated")
    parser.add_argument("--case", action="append", default=[], help="Run only the named case_id; may be repeated")
    parser.add_argument("--hammer-count", action="append", type=int, default=[], help="Run only this hammer count; may be repeated")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    try:
        config = validate_and_normalize(load_json(config_path), config_path)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    run_root = make_run_root(config)
    run_root.mkdir(parents=True, exist_ok=True)
    log_path = run_root / "execution.log"
    atomic_write_json(run_root / "config_used.json", config)

    collector_path = Path(require_str(config, "collector_binary"))
    if not args.dry_run and not collector_path.exists():
        print(f"Collector binary not found: {collector_path}. Run make first.", file=sys.stderr)
        return 2

    experiment = require_dict(config, "experiment")
    repetitions = get_int(experiment, "repetitions", 10)
    timeout_seconds = get_int(experiment, "timeout_seconds_per_point", 3600)
    max_retries = get_int(experiment, "max_retries", 0)
    refresh_enabled = get_bool(experiment, "refresh_enabled", True)

    targets = list(config["targets"])
    cases = list(config["pattern_cases"])
    counts = list(experiment["hammer_counts_per_row"])

    if args.target:
        wanted = set(args.target)
        targets = [target for target in targets if target["target_id"] in wanted]
        missing = wanted - {target["target_id"] for target in targets}
        if missing:
            print(f"CONFIG ERROR: unknown target id(s): {sorted(missing)}", file=sys.stderr)
            return 2
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["case_id"] in wanted]
        missing = wanted - {case["case_id"] for case in cases}
        if missing:
            print(f"CONFIG ERROR: unknown case id(s): {sorted(missing)}", file=sys.stderr)
            return 2
    if args.hammer_count:
        wanted_counts = set(args.hammer_count)
        counts = [count for count in counts if count in wanted_counts]
        missing_counts = wanted_counts - set(counts)
        if missing_counts:
            print(f"CONFIG ERROR: hammer count(s) not present in config: {sorted(missing_counts)}", file=sys.stderr)
            return 2

    total_points = len(targets) * len(cases) * len(counts)
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "status": "planned" if args.dry_run else "running",
        "created_at_utc": utc_now(),
        "run_root": str(run_root),
        "dimm_id": require_str(require_dict(config, "dimm"), "dimm_id"),
        "run_id": require_str(require_dict(config, "run"), "run_id"),
        "targets": targets,
        "pattern_cases": cases,
        "hammer_counts_per_row": counts,
        "repetitions_per_point": repetitions,
        "total_points": total_points,
        "refresh_enabled": refresh_enabled,
        "collector_binary": str(collector_path),
        "dataset_purpose": "controlled DRAM RowHammer characterization",
    }
    if collector_path.exists():
        manifest["collector_sha256"] = sha256_file(collector_path)
    atomic_write_json(run_root / "manifest.json", manifest)

    append_log(log_path, f"Run root: {run_root}")
    append_log(log_path, f"Planned points: {total_points}")
    if not refresh_enabled:
        append_log(log_path, "WARNING: refresh is disabled; retention failures can confound RowHammer results")

    completed_points = 0
    failed_points = 0
    skipped_points = 0

    for target in targets:
        target_dir = run_root / target_dir_name(target)
        for case in cases:
            case_dir = target_dir / case_dir_name(case)
            for count in counts:
                point_dir = case_dir / count_dir_name(count)
                point_dir.mkdir(parents=True, exist_ok=True)

                if point_complete(point_dir, repetitions) and not args.force:
                    skipped_points += 1
                    append_log(log_path, f"Skipping complete point: {point_dir}")
                    continue

                if args.force:
                    for rep in range(repetitions):
                        marker = point_dir / f"REP_{rep:02d}" / ".complete"
                        if marker.exists():
                            marker.unlink()
                archive_incomplete_repetitions(point_dir, repetitions, log_path)

                command = command_for_point(config, point_dir, target, case, count)
                point_manifest = {
                    "target": target,
                    "case": case,
                    "hammer_count_per_row": count,
                    "total_hammer_activations": count * len(target["aggressor_rows"]),
                    "repetitions": repetitions,
                    "refresh_enabled": refresh_enabled,
                    "temperature_c": require_number(require_dict(config, "environment"), "temperature_c"),
                    "voltage_vdd_v": require_number(require_dict(config, "environment"), "voltage_vdd_v"),
                    "command": command,
                }
                atomic_write_json(point_dir / "point_manifest.json", point_manifest)

                if args.dry_run:
                    append_log(log_path, f"DRY RUN: {' '.join(command)}")
                    continue

                ok = run_command(command, point_dir, log_path, timeout_seconds, max_retries)
                if ok and point_complete(point_dir, repetitions):
                    completed_points += 1
                else:
                    failed_points += 1
                    append_log(log_path, "Partial data was preserved; continuing to the next point")

    manifest.update(
        {
            "status": "planned" if args.dry_run else ("complete_with_failures" if failed_points else "complete"),
            "finished_at_utc": utc_now(),
            "completed_points": completed_points,
            "failed_points": failed_points,
            "skipped_points": skipped_points,
        }
    )
    atomic_write_json(run_root / "manifest.json", manifest)
    append_log(
        log_path,
        f"Finished: completed={completed_points}, failed={failed_points}, skipped={skipped_points}, dry_run={args.dry_run}",
    )
    return 1 if failed_points else 0


if __name__ == "__main__":
    raise SystemExit(main())
