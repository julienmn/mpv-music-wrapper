# Changelog

## v1.2.0 — 2025-12-19
- Cover selection overhaul aligned with documented spec: explicit buckets, portrait allowed in bucket 1, squarish tolerance now configurable (default 13%), album-name overlap threshold configurable (default 50%), and tiny threshold lowered to 90k.
- Added full cover selection specification (`cover_selection_spec.md`) and linked it from the README; keywords are token-based and include `insert` as non-front.
- Improved cover selection tests and tooling to dump real candidates for TDD-style fixes.
- Normalization and logging refinements carried forward from the refactor stream (loudnorm pipeline robustness, clearer RG/next-track logs).

## v1.1.0 — 2025-12-03
- Initial Python rewrite release with mpv wrapper parity, RAM staging, optional loudnorm ReplayGain, and baseline cover art handling.
