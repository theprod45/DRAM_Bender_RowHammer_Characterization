#!/usr/bin/env python3
"""Estimate RowHammer dataset size and measurement counts before a run.

The raw binary size is exact for the configured geometry. CSV sizes are estimates
because row lengths depend on identifiers, addresses, and how many bits actually flip.
This script never accesses DRAM hardware.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def human_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:,.2f} {unit}"
        size /= 1024.0
    return f"{size:,.2f} TiB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Estimate DRAM-Bender RowHammer dataset size")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--csv-bytes-per-bit-row",
        type=int,
        default=220,
        help="Approximate bytes per observations/flips CSV bit row (default: 220)",
    )
    parser.add_argument(
        "--expected-flip-rate",
        type=float,
        default=None,
        help="Optional expected mismatch fraction, e.g. 0.00001 for 0.001%%",
    )
    args = parser.parse_args()

    if args.csv_bytes_per_bit_row <= 0:
        parser.error("--csv-bytes-per-bit-row must be positive")
    if args.expected_flip_rate is not None and not (0.0 <= args.expected_flip_rate <= 1.0):
        parser.error("--expected-flip-rate must be between 0 and 1")

    with args.config.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)

    geometry = cfg.get("geometry", {})
    experiment = cfg.get("experiment", {})
    targets = cfg.get("targets", [])
    cases = cfg.get("pattern_cases", [])
    counts = experiment.get("hammer_counts_per_row", [])

    cache_lines = int(geometry.get("cache_lines_per_row", 128))
    cache_line_bytes = int(geometry.get("cache_line_bytes", 64))
    repetitions = int(experiment.get("repetitions", 10))
    verify_before = bool(experiment.get("verify_before_hammer", True))
    observations_csv = bool(experiment.get("write_observations_csv", False))

    row_bytes = cache_lines * cache_line_bytes
    bits_per_row = row_bytes * 8
    phases = 2 if verify_before else 1

    target_count = len(targets)
    victim_rows_total = sum(len(t.get("victim_rows", [])) for t in targets)
    unique_victim_coords = {
        (t.get("rank"), t.get("bank_group"), t.get("bank"), row)
        for t in targets
        for row in t.get("victim_rows", [])
    }
    unique_victim_rows = len(unique_victim_coords)

    point_count = target_count * len(cases) * len(counts)
    trial_count = point_count * repetitions

    # A target may contain more than one victim row, so weight each target by its victim count.
    victim_row_trials = victim_rows_total * len(cases) * len(counts) * repetitions
    post_hammer_bit_observations = victim_row_trials * bits_per_row
    total_bit_comparisons = post_hammer_bit_observations * phases
    raw_bytes = victim_row_trials * row_bytes * phases
    read_index_rows = victim_row_trials * cache_lines * phases

    # Approximate read_index.csv at ~180 bytes/data row plus a small header per file.
    approx_index_bytes = read_index_rows * 180
    trial_file_count = trial_count
    approx_small_metadata = trial_file_count * 4096  # trial/json/csv headers/markers, intentionally rough

    obs_rows = total_bit_comparisons if observations_csv else 0
    approx_obs_bytes = obs_rows * args.csv_bytes_per_bit_row

    print("RowHammer dataset plan")
    print("======================")
    print(f"Config:                         {args.config}")
    print(f"Targets:                        {target_count:,}")
    print(f"Victim rows across targets:     {victim_rows_total:,}")
    print(f"Unique victim row coordinates:  {unique_victim_rows:,}")
    print(f"Bits per victim row:            {bits_per_row:,}")
    print(f"Distinct victim bits/condition: {unique_victim_rows * bits_per_row:,}")
    print(f"Pattern cases:                  {len(cases):,}")
    print(f"Hammer-count settings:          {len(counts):,}")
    print(f"Repetitions per point:          {repetitions:,}")
    print(f"Experiment points:              {point_count:,}")
    print(f"Collector repetitions/trials:   {trial_count:,}")
    print(f"Victim-row trial readouts:       {victim_row_trials:,}")
    print(f"Read phases per repetition:     {phases} ({'baseline + post-hammer' if verify_before else 'post-hammer only'})")
    print()
    print("Measurements")
    print("------------")
    print(f"Post-hammer bit observations:   {post_hammer_bit_observations:,}")
    print(f"All bit comparisons:            {total_bit_comparisons:,}")
    print(f"Read-index CSV rows:            {read_index_rows:,}")
    if observations_csv:
        print(f"Full observations CSV rows:     {obs_rows:,}")
    else:
        print("Full observations CSV rows:     disabled (write_observations_csv=false)")
    print()
    print("Storage")
    print("-------")
    print(f"Raw victim readback (.bin):     {human_bytes(raw_bytes)}  [exact]")
    print(f"Read-index CSVs:                {human_bytes(approx_index_bytes)}  [rough estimate]")
    print(f"Small metadata/log overhead:    {human_bytes(approx_small_metadata)}  [rough estimate]")
    if observations_csv:
        print(f"Observations CSV:               {human_bytes(approx_obs_bytes)}  [approx @ {args.csv_bytes_per_bit_row} B/row]")
    else:
        print("Observations CSV:               0 B (disabled)")

    worst_case_flip_bytes = total_bit_comparisons * args.csv_bytes_per_bit_row
    print(f"Flip CSV theoretical maximum:   {human_bytes(worst_case_flip_bytes)}  [if every compared bit mismatched]")
    if args.expected_flip_rate is not None:
        expected_flip_rows = round(total_bit_comparisons * args.expected_flip_rate)
        expected_flip_bytes = expected_flip_rows * args.csv_bytes_per_bit_row
        print(
            f"Flip CSV at {args.expected_flip_rate:.6%}:      "
            f"~{expected_flip_rows:,} rows / {human_bytes(expected_flip_bytes)}"
        )

    base_estimate = raw_bytes + approx_index_bytes + approx_small_metadata + approx_obs_bytes
    print()
    print(f"Base estimated dataset size:    {human_bytes(base_estimate)} + actual flips.csv/log growth")
    print()
    print("Note: RowHammer targets rows. The collector reads every bit in each configured victim row;")
    print("      it does not hammer or sample a single individual bit in isolation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
