# Changelog

## 0.1.1 - Documentation and validation update

- Clarified alignment with the study plan: 10-repeat default, multi-victim target generation, environment logging, flip direction, and boundary/edge tagging via target notes.
- Revalidated the Python configuration/runner path with a full dry-run.
- Rechecked the C++ source against the current DRAM-Bender API signatures used by the U200 examples.

## 0.1.0 - Initial research collector

- Added configurable DRAM-Bender C++ RowHammer collector for Alveo U200 DDR4.
- Added explicit victim-row and aggressor-row selection.
- Added single-, double-, and arbitrary multi-aggressor support through ordered row lists.
- Added configurable hammer-count sweeps and data-pattern cases.
- Added baseline verification before hammering to separate setup/timing errors from post-hammer errors.
- Added raw victim readbacks, address indexes, bit-level flip CSVs, trial metadata, and hammer sequence metadata.
- Added dry-run/resume orchestration, simple target generator, and dataset summary script.
