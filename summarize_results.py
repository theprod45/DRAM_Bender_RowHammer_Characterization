#!/usr/bin/env python3
"""Create a compact CSV summary from RowHammer trial.json files."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable


FIELDS = [
    "dimm_id",
    "run_id",
    "target_id",
    "case_id",
    "repetition",
    "rank",
    "bank_group",
    "bank",
    "victim_rows",
    "aggressor_rows",
    "victim_pattern_hex",
    "aggressor_pattern_hex",
    "hammer_count_per_row",
    "total_hammer_activations",
    "refresh_enabled",
    "temperature_c",
    "voltage_vdd_v",
    "baseline_flip_count",
    "post_hammer_flip_count",
    "new_flip_count_upper_bound",
    "hammer_elapsed_seconds_host",
    "status",
    "trial_path",
]


def flatten(trial: Dict[str, Any], path: Path) -> Dict[str, Any]:
    baseline = int(trial.get("baseline_flip_count", 0) or 0)
    post = int(trial.get("post_hammer_flip_count", 0) or 0)
    # This is only a count-level upper-bound. To identify genuinely new bit
    # locations, compare baseline_flips.csv and flips.csv by their address/bit key.
    return {
        "dimm_id": trial.get("dimm_id", ""),
        "run_id": trial.get("run_id", ""),
        "target_id": trial.get("target_id", ""),
        "case_id": trial.get("case_id", ""),
        "repetition": trial.get("repetition", ""),
        "rank": trial.get("rank", ""),
        "bank_group": trial.get("bank_group", ""),
        "bank": trial.get("bank", ""),
        "victim_rows": "|".join(str(v) for v in trial.get("victim_rows", [])),
        "aggressor_rows": "|".join(str(v) for v in trial.get("aggressor_rows", [])),
        "victim_pattern_hex": trial.get("victim_pattern_hex", ""),
        "aggressor_pattern_hex": trial.get("aggressor_pattern_hex", ""),
        "hammer_count_per_row": trial.get("hammer_count_per_row", ""),
        "total_hammer_activations": trial.get("total_hammer_activations", ""),
        "refresh_enabled": trial.get("refresh_enabled", ""),
        "temperature_c": trial.get("temperature_c", ""),
        "voltage_vdd_v": trial.get("voltage_vdd_v", ""),
        "baseline_flip_count": baseline,
        "post_hammer_flip_count": post,
        "new_flip_count_upper_bound": max(0, post - baseline),
        "hammer_elapsed_seconds_host": trial.get("hammer_elapsed_seconds_host", ""),
        "status": trial.get("status", ""),
        "trial_path": str(path),
    }


def iter_trials(root: Path) -> Iterable[tuple[Path, Dict[str, Any]]]:
    for path in sorted(root.rglob("trial.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                trial = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARNING: skipping {path}: {exc}")
            continue
        if isinstance(trial, dict):
            yield path, trial


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize DRAM-Bender RowHammer characterization results")
    parser.add_argument("root", type=Path, help="Run root or output_root to scan recursively")
    parser.add_argument("--output", type=Path, default=Path("rowhammer_summary.csv"))
    args = parser.parse_args()

    rows = [flatten(trial, path) for path, trial in iter_trials(args.root)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {args.output} with {len(rows)} trial(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
