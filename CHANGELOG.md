# Changelog

All notable changes to **VDatum Batch Transform** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.2] - 8/12/2026

### Changed
- README copyedit: typo and wording fixes throughout (pipeline, sign
  convention, and QC sections). Documentation only; no code or behavior change.

## [2.0.1] - 8/12/2026

### Changed
- Switched release dates in `CHANGELOG.md` and `RELEASE_NOTES.md` from
  year-first `YYYY-MM-DD` to `M/D/YYYY` (e.g. `8/12/2026`). Documentation only;
  no code or behavior change.

## [2.0.0] - 8/12/2026

Major redesign of the pipeline and output schema. **Breaking** changes to the
config keys and CSV columns.

### Added
- VDatum now models the geodetic value (NAVD88) whenever CO-OPS lacks it, using
  the tidal datum as the 0 reference plane (`s_z = 0` → NAVD88). This fills the
  many stations that previously had no result (the old `NO_TIDAL` category).
- Tidal datum is now selectable in `[datums] tidal_datum`: `MLLW`, `MLW`, `MHW`,
  `MHHW`, or `LMSL` (LMSL maps to CO-OPS `MSL` automatically).
- When CO-OPS has no tidal value, `ST_<TIDAL>` is written as `0` with source
  `VDATUM_ZERO` and an explanatory note (the tidal datum is the 0 plane, not an
  observed value). Observed tidal values remain CO-OPS-only.
- Internal QC cross-check (`[qc] crosscheck_coops`, default on): when both the
  tidal datum and NAVD88 come from CO-OPS, VDatum's modeled value is compared;
  a note flags disagreement beyond VDatum's reported uncertainty. Advisory only
  — CO-OPS values are always retained and the check never affects the "VDatum
  down" verdict.

### Changed
- **Output schema** simplified to: `ST_<TIDAL>`, `<TIDAL>_Source`, `ST_NAVD88`,
  `NAVD88_Source`, `VDatum_uncertainty`, `Notes` (plus pass-through + id/lat/lon).
  The per-target column blocks and the separate raw/region columns were removed.
- **Two output CSVs** now split by outcome: `<basename>.csv` holds stations that
  obtained NAVD88 (CO-OPS or VDatum); `<basename>_exceptions.csv` holds stations
  with no CO-OPS NAVD88 **and** out of VDatum range.
- VDatum values are sign-flipped to the CO-OPS up-is-positive convention in
  `ST_NAVD88` (documented, with a link to the VDatum FAQ).
- `--recheck-failures` now re-runs stations that landed in exceptions due to a
  VDatum server/API error, merging successes back into the split output.

### Removed
- Multiple-geodetic-target support (`geodetic_datums` list) and the associated
  per-target columns. The tool now does one tidal datum → one geodetic datum.

## [1.0.3] - 8/12/2026

### Changed
- Documented the "one file per run" input contract in `README.md` (Input
  format), `data/README.md`, and the `config.ini [input]` section (the tool
  processes only the file named in `[input] file`; run once per file with a
  distinct `[output] basename` to process several sheets). No behavior change.

  (Supersedes an incomplete v1.0.2 tag that omitted the README change.)

## [1.0.1] - 8/12/2026

### Added
- Shipped empty `data/` and `output/` directories, each with a `README.md`, so
  users don't have to create them before the first run (Git can't track empty
  directories). The READMEs document the required input columns / accepted
  formats and the generated output files. Working data and results remain
  gitignored.
- `RELEASE_NOTES_v1.0.0.md` and `CHANGELOG.md` added to the repo.

### Changed
- Documentation/metadata edits to `README.md` and `config.ini`.

## [1.0.0] - 8/12/2026

First public release. Batch-transforms tidal datums to geodetic datums for a
list of NOAA CO-OPS tide stations, using the CO-OPS Tides & Currents Metadata
API with a NOAA VDatum API fallback. INI-configured; read-only source; CSV
output.

### Added
- T&C-first, VDatum-fallback resolution per station, recording both sign
  conventions (VDatum up-is-negative raw value and the T&C up-is-positive value).
- Multiple geodetic targets: one tidal zero-plane, one or more geodetic targets,
  each with its own output column block.
- Flexible input formats: `.csv`, `.ods`, and `.xlsx`, with configurable
  required/optional columns and clear validation errors.
- Smart region fallthrough that distinguishes a region not covering a point from
  a genuine VDatum server fault, including the `region:FRAME` override for grids
  requiring a specific input frame (e.g. `chesapeak_delaware:IGS14`).
- Resilience to VDatum instability: retry-with-backoff for transient "Uncaught
  error" faults; a canary health check (startup / first-error / end-of-run); and
  a "VDatum likely down" verdict with a non-zero exit code (3) for scripting.
- VDatum bug report: persistent server faults written to a separate CSV with the
  exact failing URLs, ready to forward to the NOAA VDatum Program Support team.
- `--recheck-failures` mode: re-run only the targets previously marked
  `VDATUM_API_ERROR`, merging successes back into the existing output.
- Full per-run logging of every API request and response.
- Example dataset (`examples/`) and documentation (`README.md`).

[Unreleased]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v2.0.2...HEAD
[2.0.2]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.3...v2.0.0
[1.0.3]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.1...v1.0.3
[1.0.1]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/namurry-noaa/VDatum_Batch_Transform/releases/tag/v1.0.0
