# DRAM-Bender RowHammer Characterization Dataset Collector

This repository contains a configurable RowHammer **characterization** workload for DRAM Bender, designed to produce a structured dataset in the same general style as the companion DRAM-Bender latency-PUF collector.

It is intended for controlled experiments on DRAM modules connected to an FPGA test platform. It does not contain operating-system privilege-escalation code, page-table targeting, sandbox escape logic, cache-bypass tricks, or any mechanism for attacking another process. The experiment works directly with DRAM row coordinates supplied by the researcher.

## What it records

For each configured target, data-pattern case, hammer count, and repetition, the collector:

1. Writes the configured victim row(s) with the victim pattern.
2. Writes the configured aggressor row(s) with the aggressor pattern.
3. Optionally reads the victim row(s) **before hammering** to detect setup/timing errors.
4. Repeatedly activates the configured aggressor rows in their listed order.
5. Reads the victim row(s) after hammering using nominal read timings.
6. Stores complete raw victim readback data and bit-level mismatch records.

The resulting dataset can therefore distinguish:

- cells that were already incorrect before hammering;
- cells that are incorrect after hammering;
- flip direction (`0_to_1` or `1_to_0`);
- the exact victim row/cache-line/bit coordinate;
- the aggressor rows and hammer count used for that observation.

The address fields intentionally resemble the latency-PUF dataset so the two experiments can later be correlated using fields such as:

```text
rank, bank_group, bank, row, cache_line, bit_index_in_row
```

### Alignment with the current characterization plan

The companion experiment plan calls for a minimum of **10 distinct victim locations × 10 repeats per condition**, separate handling of cells near physical-array boundaries, environment logging, and per-bit expected/observed values with flip direction. This repository is set up to support that workflow:

- `experiment.repetitions` defaults to `10`;
- `generate_config.py --victim-rows ...` can create any number of explicit victim targets, including 10 or more;
- temperature and voltage metadata are stored with the run and bit-level records;
- baseline and post-hammer CSVs record expected bit, observed bit, and `0_to_1` / `1_to_0` direction;
- target `notes` are preserved in the run manifests, so suspected boundary/edge locations can be tagged, or they can simply be excluded from `targets`.

`config.example.json` intentionally contains only two demonstration targets so that copying the example does not silently schedule a large experiment. For the real study, generate or enter the full victim set explicitly. Physical-array edge status cannot be inferred reliably from a logical row number alone unless the DIMM/bitstream mapping has been validated.

## Repository contents

- `rowhammer_collector.cpp` — native DRAM-Bender C++ collector.
- `run_experiment.py` — JSON-driven experiment runner with dry-run, resume, filters, timeout handling, and partial-data preservation.
- `config.example.json` — complete example configuration.
- `generate_config.py` — helper for generating common single- or double-sided targets from victim rows.
- `summarize_results.py` — creates a compact CSV summary from `trial.json` files.
- `Makefile` — follows the DRAM-Bender `sources/apps/...` build layout.
- `CHANGELOG.md` — project changes.

## DRAM-Bender compatibility

This first version targets the current DRAM-Bender C++ API and the Alveo U200 DDR4 prototype, matching the structure used by the latency-PUF collector.

The collector uses the published DRAM-Bender primitives:

- `SMC_PRE`
- `SMC_ACT`
- `SMC_WRITE`
- `SMC_READ`
- `SMC_NOP`
- `SMC_SLEEP`
- `SMC_LI`
- `SMC_ADDI`
- `Program::add_branch`
- `SoftMCPlatform::execute`
- `SoftMCPlatform::receiveData`
- `SoftMCPlatform::reset_fpga`
- `SoftMCPlatform::set_aref`

The hammer loop follows the same general ACT/PRE-and-branch approach used by DRAM Bender's official double-sided RowHammer example, but generalizes it to an arbitrary ordered list of aggressor rows.

References:

- DRAM Bender: https://github.com/CMU-SAFARI/DRAM-Bender
- DRAM Bender Python RowHammer example: `sources/api/pySoftMC/example.py`
- U-TRR RowHammer characterization tools: https://github.com/CMU-SAFARI/U-TRR

## Install inside DRAM Bender

Copy the repository directory into:

```text
DRAM-Bender/sources/apps/RowHammerCharacterization/
```

Expected layout:

```text
DRAM-Bender/
├── boost-lib/
└── sources/
    ├── api/
    └── apps/
        └── RowHammerCharacterization/
            ├── Makefile
            ├── rowhammer_collector.cpp
            ├── run_experiment.py
            ├── config.example.json
            ├── generate_config.py
            ├── summarize_results.py
            └── README.md
```

Build:

```bash
cd DRAM-Bender/sources/apps/RowHammerCharacterization
make
```

Before running reduced-timing or hammer experiments, first verify the board/DIMM with DRAM Bender's normal `Smalltest` and ensure the correct U200 bitstream and XDMA driver are loaded.

## Quick start

Create a working configuration:

```bash
cp config.example.json config.json
nano config.json
```

Validate the plan without touching hardware:

```bash
python3 -m json.tool config.json > /dev/null
python3 run_experiment.py --config config.json --dry-run
```

Run the configured experiment:

```bash
python3 run_experiment.py --config config.json
```

The runner resumes completed points automatically. Use `--force` if you deliberately want to rerun them.

Useful filters:

```bash
python3 run_experiment.py --config config.json --target DOUBLE_SIDED_ROW_1000
python3 run_experiment.py --config config.json --case V00_AFF
python3 run_experiment.py --config config.json --hammer-count 100000
```

Filters can be combined.

## Configuring victim and aggressor rows

Each target explicitly names the DRAM coordinates and the rows to test:

```json
{
  "target_id": "DOUBLE_SIDED_ROW_1000",
  "rank": 0,
  "bank_group": 0,
  "bank": 0,
  "victim_rows": [1000],
  "aggressor_rows": [999, 1001]
}
```

The collector activates aggressors in the exact order listed in `aggressor_rows`, then repeats that ordered sequence until every aggressor has received `hammer_count_per_row` activations.

Examples:

### Single-sided

```json
"victim_rows": [1000],
"aggressor_rows": [999]
```

### Double-sided

```json
"victim_rows": [1000],
"aggressor_rows": [999, 1001]
```

### Arbitrary multi-sided sequence

```json
"victim_rows": [1000],
"aggressor_rows": [998, 999, 1001, 1002]
```

All victim and aggressor rows in a target use the same configured rank, bank group, and bank.

### Important row-mapping note

`row - 1` and `row + 1` are only **logical row numbers presented to DRAM Bender**. Internal DRAM remapping, repaired rows, vendor-specific organization, and the selected FPGA/PHY address mapping can affect whether those are physically adjacent cells. Treat adjacency as something to validate experimentally rather than assume from the number alone.

## Generate common targets automatically

For several victim rows using a standard double-sided pattern:

```bash
python3 generate_config.py \
  --victim-rows 1000,2000,3000 \
  --mode double-sided \
  --distance 1 \
  --rank 0 \
  --bank-group 0 \
  --bank 0 \
  --hammer-counts 10000,50000,100000,250000 \
  --output config.json
```

Single-sided modes are also available:

```text
--mode single-left
--mode single-right
```

The generated rows should still be checked against the valid address range and mapping of the installed DIMM/bitstream.

## Hammer-count sweep

The experiment section accepts any list of positive 32-bit activation counts:

```json
"hammer_counts_per_row": [10000, 50000, 100000, 250000]
```

For a double-sided target with two aggressors and:

```text
hammer_count_per_row = 100000
```

each aggressor is activated 100,000 times, so the total number of ACT commands attributed to the configured aggressors is 200,000.

The exact value is stored in every `trial.json` and bit-level CSV row.

## Data-pattern cases

Pattern cases are independent from target coordinates:

```json
"pattern_cases": [
  {
    "case_id": "V00_AFF",
    "victim_pattern": "00",
    "aggressor_pattern": "FF"
  },
  {
    "case_id": "VFF_A00",
    "victim_pattern": "FF",
    "aggressor_pattern": "00"
  }
]
```

The example configuration also includes `AA/55` and `55/AA` cases.

Using opposite victim/aggressor patterns makes direction-dependent failures easier to characterize, while the stored bit-level records still report the actual expected and observed bit values.

## Timing parameters

The collector deliberately separates **safe initialization/readout timing** from **hammer-loop timing**.

### Safe initialization/readout

```json
"nominal_trcd_slots": 9,
"nominal_trp_slots": 9,
"slot_ns_metadata": 1.5,
"write_spacing_slots": 7,
"read_spacing_slots": 7,
"write_recovery_slots": 8,
"read_to_precharge_slots": 8,
"final_guard_slots": 16
```

These `_slots` fields use the same convention as the latency-PUF collector: a slot is one DDR command position in DRAM Bender's packed four-command instruction stream. On the standard U200 DDR4 design this is normally treated as 1.5 ns metadata.

Keep these timings known-good. The RowHammer experiment should not accidentally become a reduced-tRCD/tRP latency-violation experiment.

### Hammer loop

```json
"hammer_act_to_pre_cycles": 5,
"hammer_pre_to_act_cycles": 3,
"hammer_final_guard_cycles": 3,
"fabric_cycle_ns_metadata": 6.0
```

These values are **fabric cycles**, not the 1.5 ns command slots above. They are used with `SMC_SLEEP`/full-NOP instructions around the ACT/PRE loop.

The defaults mirror the scale used in DRAM Bender's published Python RowHammer example. They should still be checked against the real DIMM timing requirements and the bitstream in use.

## Refresh behavior

The default configuration keeps refresh enabled:

```json
"refresh_enabled": true
```

The collector applies this with:

```cpp
platform.set_aref(true);
```

This is the recommended setting for the primary RowHammer characterization dataset because turning refresh off can introduce retention failures that are not caused by hammering.

You may set refresh to `false` for a separate controlled comparison, but the runner prints a warning and the resulting dataset records `refresh_enabled=0` everywhere.

### Exact refresh-cadence limitation

This collector currently relies on DRAM Bender's platform auto-refresh control rather than synthesizing a manual `SMC_REF` schedule at a precisely modeled tREFI interval. If your experiment requires cycle-accurate refresh placement during a long hammer loop, validate the maintenance-controller behavior on your installed DRAM-Bender version or adapt the explicit refresh scheduling approach used by U-TRR.

## Baseline verification

Recommended default:

```json
"verify_before_hammer": true
```

For every repetition the collector initializes the rows and then reads the victim before hammering.

This creates:

```text
baseline_reads.bin
baseline_read_index.csv
baseline_flips.csv
```

If a cell is already wrong in `baseline_flips.csv`, it should not automatically be treated as a RowHammer-induced failure.

After hammering, the main files are:

```text
reads.bin
read_index.csv
flips.csv
```

`flips.csv` contains only mismatching bits. Set:

```json
"write_observations_csv": true
```

if you also want one CSV row for every tested bit. This can become very large.

## Dataset layout

Example:

```text
dram_rowhammer_data/
└── DIMM_DIMM_001/
    └── RUN_rowhammer_room_temp_001__TEMP_22C__VDD_1p2V/
        ├── manifest.json
        ├── config_used.json
        ├── execution.log
        └── TARGET_DOUBLE_SIDED_ROW_1000__R0__BG0__B0/
            └── CASE_V00_AFF__V00__AFF/
                └── HAMMER_0000100000_PER_ROW/
                    ├── point_manifest.json
                    ├── point_status.json
                    ├── collector_attempt_01.stdout.log
                    ├── collector_attempt_01.stderr.log
                    ├── REP_00/
                    │   ├── trial.json
                    │   ├── hammer_sequence.csv
                    │   ├── baseline_reads.bin
                    │   ├── baseline_read_index.csv
                    │   ├── baseline_flips.csv
                    │   ├── reads.bin
                    │   ├── read_index.csv
                    │   ├── flips.csv
                    │   └── .complete
                    └── REP_01/
                        └── ...
```

## `flips.csv` fields

The post-hammer and baseline flip CSVs contain fields including:

```text
dimm_id
run_id
target_id
case_id
phase
victim_pattern_hex
aggressor_pattern_hex
hammer_count_per_row
total_hammer_activations
aggressor_rows
repetition
temperature_c
voltage_vdd_v
refresh_enabled
rank
bank_group
bank
flat_bank
row
cache_line
column
byte_in_cache_line
bit_in_byte
bit_index_in_cache_line
bit_index_in_row
raw_byte_offset
expected_bit
observed_bit
did_flip
flip_direction
```

`phase` is either:

```text
baseline
post_hammer
```

This makes it possible to compare the same bit coordinate before and after hammering.

## Summarize a dataset

After a run:

```bash
python3 summarize_results.py ./dram_rowhammer_data --output rowhammer_summary.csv
```

The summary contains one row per repetition with the target, patterns, requested activation counts, baseline flip count, post-hammer flip count, and run metadata.

For scientifically identifying **newly flipped bit locations**, compare `baseline_flips.csv` and `flips.csv` using the full address/bit key rather than only subtracting total counts.

## Suggested characterization workflow

A useful first pass is:

1. Run DRAM Bender `Smalltest` with nominal timings.
2. Start with one known-valid bank and a small number of victim locations.
3. Keep `refresh_enabled=true`.
4. Keep nominal read/write timings fixed and known-good.
5. Sweep hammer count from low to high.
6. Repeat each condition multiple times.
7. Test opposite data polarities (`00/FF`, `FF/00`, `AA/55`, `55/AA`).
8. Record temperature and voltage for every run.
9. Treat any baseline failures separately.
10. Expand to more victim rows only after verifying the address geometry and runtime.

## Correlating with the latency-PUF dataset

The RowHammer dataset is intentionally structured so it can later be compared with the latency-PUF dataset.

A practical bit-level join key is:

```text
dimm_id
rank
bank_group
bank
row
cache_line
bit_index_in_row
```

Then additional analysis can ask questions such as:

- Are latency-PUF unstable bits also RowHammer-sensitive?
- Does flip direction agree between the two mechanisms?
- Are RowHammer-sensitive cells clustered around latency-sensitive locations?
- Does correlation increase at more aggressive latency thresholds or higher hammer counts?

Do not assume that a logical-row neighborhood is the same as the physical cell neighborhood until the DIMM mapping has been validated.

## Notes on experiment interpretation

A post-hammer mismatch is evidence that the stored value changed during the experiment, but interpreting the cause still requires controls. In particular:

- baseline failures can come from setup or timing problems;
- disabling refresh can introduce retention errors;
- excessive ACT/PRE timing reduction can create command-timing failures rather than classical RowHammer disturbance;
- internal row remapping can complicate adjacency assumptions;
- vendor mitigation such as TRR can suppress or reshape observed behavior.

For this reason, the dataset stores the relevant experimental context rather than only a final flip count.

## Hardware caution

Run this only on DRAM modules and FPGA systems you are authorized to test. Start with conservative hammer counts and verified timings, keep logs, and make sure the board can be reset cleanly if an experiment is interrupted.
