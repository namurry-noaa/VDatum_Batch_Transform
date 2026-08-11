# Changelog

All notable changes to **VDatum Batch Transform** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.3] - 2026-08-12

### Changed
- Documented the "one file per run" input contract in `README.md` (Input
  format), `data/README.md`, and the `config.ini [input]` section (the tool
  processes only the file named in `[input] file`; run once per file with a
  distinct `[output] basename` to process several sheets). No behavior change.

  (Supersedes an incomplete v1.0.2 tag that omitted the README change.)

## [1.0.1] - 2026-08-12

### Added
- Shipped empty `data/` and `output/` directories, each with a `README.md`, so
  users don't have to create them before the first run (Git can't track empty
  directories). The READMEs document the required input columns / accepted
  formats and the generated output files. Working data and results remain
  gitignored.
- `RELEASE_NOTES_v1.0.0.md` and `CHANGELOG.md` added to the repo.

### Changed
- Documentation/metadata edits to `README.md` and `config.ini`.

## [1.0.0] - 2026-08-12

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

[Unreleased]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.1...v1.0.3
[1.0.1]: https://github.com/namurry-noaa/VDatum_Batch_Transform/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/namurry-noaa/VDatum_Batch_Transform/releases/tag/v1.0.0
