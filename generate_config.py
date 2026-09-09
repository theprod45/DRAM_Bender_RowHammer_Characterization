#!/usr/bin/env python3
"""Generate RowHammer target configurations.

Victims can be supplied explicitly, or sampled across a logical bank range.
This helper only writes JSON configuration files; it does not access hardware.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import List


def parse_csv_ints(text: str) -> List[int]:
    values: List[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token, 0)
        if value < 0:
            raise argparse.ArgumentTypeError("row/count values must be non-negative")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return values


def choose_bin_centres(first_row: int, last_row: int, count: int) -> List[int]:
    """Choose one logical row near the centre of each equal-width bank interval."""
    if count <= 0:
        raise ValueError("victim count must be positive")
    span = last_row - first_row + 1
    if count > span:
        raise ValueError("victim count exceeds the number of available logical rows")

    rows: List[int] = []
    for i in range(count):
        # Integer form of first + ((i + 0.5) / count) * span.
        row = first_row + ((2 * i + 1) * span) // (2 * count)
        row = min(row, last_row)
        if rows and row <= rows[-1]:
            row = rows[-1] + 1
        if row > last_row:
            raise ValueError("could not place unique victim rows in the requested range")
        rows.append(row)
    return rows


def choose_bit_intervals(first_row: int, last_row: int, interval_bits: int, bits_per_row: int) -> List[int]:
    """Choose logical victim rows approximately every interval_bits across a bank range.

    RowHammer targets rows, not individual cells, so the requested bit interval is rounded
    upward to an integral number of complete DRAM rows. The first victim is placed near the
    centre of the first interval to avoid clustering at the lower bank edge.
    """
    if interval_bits <= 0:
        raise ValueError("interval bits must be positive")
    if bits_per_row <= 0:
        raise ValueError("bits per row must be positive")

    step_rows = max(1, math.ceil(interval_bits / bits_per_row))
    start = first_row + step_rows // 2
    if start > last_row:
        start = (first_row + last_row) // 2
    return list(range(start, last_row + 1, step_rows))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate RowHammer config targets")
    parser.add_argument("--template", type=Path, default=Path(__file__).with_name("config.example.json"))
    parser.add_argument("--output", type=Path, default=Path("config.json"))

    parser.add_argument("--victim-rows", type=parse_csv_ints, default=None,
                        help="Explicit comma-separated victim rows")
    parser.add_argument("--first-row", type=int, default=None,
                        help="First logical row in the bank/range to sample")
    parser.add_argument("--last-row", type=int, default=None,
                        help="Last logical row in the bank/range to sample, inclusive")
    spread = parser.add_mutually_exclusive_group()
    spread.add_argument("--victim-count", type=int, default=None,
                        help="Spread this many victim rows across first-row..last-row")
    spread.add_argument("--interval-bits", type=int, default=None,
                        help="Select victims at approximately this many bits apart")

    parser.add_argument(
        "--mode",
        choices=("double-sided", "single-left", "single-right"),
        default="double-sided",
        help="How to derive aggressor rows from each victim",
    )
    parser.add_argument("--distance", type=int, default=1,
                        help="Logical row distance from victim to aggressor (default: 1)")
    parser.add_argument("--rank", type=int, default=0)
    parser.add_argument("--bank-group", type=int, default=0)
    parser.add_argument("--bank", type=int, default=0)
    parser.add_argument("--hammer-counts", type=parse_csv_ints, default=None,
                        help="Optional counts per aggressor row")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dimm-id", default=None)
    args = parser.parse_args()

    if args.distance <= 0:
        parser.error("--distance must be positive")
    if min(args.rank, args.bank_group, args.bank) < 0:
        parser.error("rank/bank coordinates must be non-negative")

    with args.template.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    geometry = config.get("geometry", {})
    cache_lines_per_row = int(geometry.get("cache_lines_per_row", 128))
    cache_line_bytes = int(geometry.get("cache_line_bytes", 64))
    bits_per_row = cache_lines_per_row * cache_line_bytes * 8

    explicit = args.victim_rows is not None
    ranged = args.first_row is not None or args.last_row is not None or args.victim_count is not None or args.interval_bits is not None
    if explicit and ranged:
        parser.error("use either --victim-rows or the range-based spread options, not both")
    if not explicit:
        if args.first_row is None or args.last_row is None:
            parser.error("spread selection requires --first-row and --last-row")
        if args.victim_count is None and args.interval_bits is None:
            parser.error("spread selection also requires --victim-count or --interval-bits")
        if args.first_row < 0 or args.last_row < args.first_row:
            parser.error("invalid first/last row range")

        # Keep enough room for derived aggressor rows.
        first_allowed = args.first_row
        last_allowed = args.last_row
        if args.mode in ("double-sided", "single-left"):
            first_allowed += args.distance
        if args.mode in ("double-sided", "single-right"):
            last_allowed -= args.distance
        if first_allowed > last_allowed:
            parser.error("row range is too small after reserving aggressor-distance margins")

        try:
            if args.victim_count is not None:
                victim_rows = choose_bin_centres(first_allowed, last_allowed, args.victim_count)
            else:
                victim_rows = choose_bit_intervals(
                    first_allowed, last_allowed, args.interval_bits, bits_per_row
                )
        except ValueError as exc:
            parser.error(str(exc))
    else:
        victim_rows = list(args.victim_rows)

    targets = []
    for victim in victim_rows:
        if args.mode == "double-sided":
            if victim < args.distance:
                parser.error(f"victim row {victim} is too close to zero for distance {args.distance}")
            aggressors = [victim - args.distance, victim + args.distance]
        elif args.mode == "single-left":
            if victim < args.distance:
                parser.error(f"victim row {victim} is too close to zero for distance {args.distance}")
            aggressors = [victim - args.distance]
        else:
            aggressors = [victim + args.distance]

        targets.append(
            {
                "target_id": f"{args.mode.upper().replace('-', '_')}_ROW_{victim}",
                "rank": args.rank,
                "bank_group": args.bank_group,
                "bank": args.bank,
                "victim_rows": [victim],
                "aggressor_rows": aggressors,
                "notes": "Generated logical-row target; confirm physical row mapping before interpreting adjacency.",
            }
        )

    config["targets"] = targets
    if args.hammer_counts is not None:
        if any(count <= 0 or count > 0xFFFFFFFF for count in args.hammer_counts):
            parser.error("hammer counts must be positive 32-bit integers")
        config["experiment"]["hammer_counts_per_row"] = args.hammer_counts
    if args.run_id:
        config["run"]["run_id"] = args.run_id
    if args.dimm_id:
        config["dimm"]["dimm_id"] = args.dimm_id

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")

    print(f"Wrote {args.output} with {len(targets)} target(s).")
    print(f"Bits per victim row: {bits_per_row:,} ({bits_per_row // 8:,} bytes)")
    if len(victim_rows) > 1:
        deltas = [victim_rows[i + 1] - victim_rows[i] for i in range(len(victim_rows) - 1)]
        print(
            "Logical row spacing: "
            f"min={min(deltas):,}, max={max(deltas):,} rows "
            f"(~{min(deltas) * bits_per_row:,} to {max(deltas) * bits_per_row:,} bits)"
        )
    for target in targets:
        print(
            f"  {target['target_id']}: victim={target['victim_rows']} "
            f"aggressors={target['aggressor_rows']} R{target['rank']} BG{target['bank_group']} B{target['bank']}"
        )
    print("Next: python3 estimate_dataset_size.py --config", args.output)
    print("Then: python3 run_experiment.py --config", args.output, "--dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
