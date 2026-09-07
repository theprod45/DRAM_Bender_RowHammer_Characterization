#!/usr/bin/env python3
"""Generate simple single- or double-sided RowHammer targets from victim rows.

This helper edits the provided config template only. It does not access hardware.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List


def parse_csv_ints(text: str) -> List[int]:
    values = []
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate RowHammer config targets from victim rows")
    parser.add_argument("--template", type=Path, default=Path(__file__).with_name("config.example.json"))
    parser.add_argument("--output", type=Path, default=Path("config.json"))
    parser.add_argument("--victim-rows", type=parse_csv_ints, required=True, help="Comma-separated victim rows")
    parser.add_argument(
        "--mode",
        choices=("double-sided", "single-left", "single-right"),
        default="double-sided",
        help="How to derive aggressor rows from each victim",
    )
    parser.add_argument("--distance", type=int, default=1, help="Logical row distance from victim (default: 1)")
    parser.add_argument("--rank", type=int, default=0)
    parser.add_argument("--bank-group", type=int, default=0)
    parser.add_argument("--bank", type=int, default=0)
    parser.add_argument("--hammer-counts", type=parse_csv_ints, default=None, help="Optional counts per aggressor row")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dimm-id", default=None)
    args = parser.parse_args()

    if args.distance <= 0:
        parser.error("--distance must be positive")
    if min(args.rank, args.bank_group, args.bank) < 0:
        parser.error("rank/bank coordinates must be non-negative")

    with args.template.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    targets = []
    for victim in args.victim_rows:
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
    for target in targets:
        print(
            f"  {target['target_id']}: victim={target['victim_rows']} "
            f"aggressors={target['aggressor_rows']} R{target['rank']} BG{target['bank_group']} B{target['bank']}"
        )
    print("Next: python3 run_experiment.py --config", args.output, "--dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
