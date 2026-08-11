# Release Notes

Notes for each tagged release, newest first. See `CHANGELOG.md` for the
terse, structured change list.

---

## v1.0.2 — 2026-08-12

Documentation patch. No functional changes.

### Changed
- Made the **one file per run** input contract explicit in `data/README.md` and
  the `config.ini [input]` section: the tool processes only the single file
  named in `[input] file` (it does not scan a directory). To process several
  sheets, run the tool once per file, each with its own `[output] basename` so
  results don't overwrite each other.

---

## v1.0.1 — 2026-08-12

Maintenance / packaging patch. No functional changes to the transform logic.

### Changed
- Ship empty `data/` and `output/` directories, each with a `README.md`, so
  users don't have to create them before the first run (Git can't track empty
  directories). The READMEs document the required input columns / accepted
  formats and the generated output files. Working data and results remain
  gitignored.
- Added `CHANGELOG.md` and consolidated release notes into this file.
- Documentation and configuration metadata edits (`README.md`, `config.ini`).

---

## v1.0.0 — 2026-08-12

First public release. Batch-transforms tidal datums to geodetic datums for a list of NOAA CO-OPS tide stations, using the CO-OPS Tides & Currents Metadata API with a NOAA VDatum API fallback.

### Overview

For each station (identified by CO-OPS Station ID + lat/lon), the tool:

1. Pulls published datums from the **CO-OPS Metadata API**.
2. Reads the configured **tidal datum** (e.g. MLLW) as the zero-plane.
3. Resolves each configured **geodetic target** (e.g. NAVD88) relative to that zero-plane — from T&C when published, otherwise via a **NOAA VDatum** transformation.

Everything is driven by a single INI config file. The source spreadsheet is treated as read-only; results are written to CSV.

### Features

- **T&C-first, VDatum-fallback** resolution per station, with both value-sign conventions recorded (VDatum's up-is-negative raw value and the T&C up-is-positive value).
- **Multiple geodetic targets** — one tidal zero-plane, one or more geodetic targets, each with its own output column block.
- **Flexible input** — `.csv`, `.ods`, or `.xlsx`; required/optional columns configurable, with clear validation errors.
- **Smart region fallthrough** — distinguishes a region that simply doesn't cover a point from a genuine VDatum server fault, including the `region:FRAME` override for grids that require a specific input frame (e.g. `chesapeak_delaware:IGS14`).
- **Resilience to a cranky VDatum API** — retry-with-backoff for transient "Uncaught error" faults, a canary health check (startup / first-error / end-of-run), and a loud "VDatum likely down" verdict with a non-zero exit code for scripting.
- **VDatum bug report** — persistent server faults are written to a separate CSV with the exact failing URLs, ready to forward to the NOAA VDatum Program Support team.
- **`--recheck-failures`** — re-run only the targets that previously failed with `VDATUM_API_ERROR`, merging successes back into the existing output (use when VDatum recovers).
- **Full logging** — every API request and response captured to a per-run logfile.

### Requirements

- Python 3.12; `pandas`, `requests`, `odfpy` (see `environment.yml`).

### Getting started

```bash
conda env create -f environment.yml
conda activate vdatum_batch_xform
# edit config.ini, then:
python vdatum_batch_transform.py --config config.ini
```

See `examples/` for a small runnable sample and `README.md` for full documentation.

### Notes

- CO-OPS throttles heavy API use; a courtesy delay between calls is configurable.
- VDatum tidal transforms extend only slightly inland of the shoreline; far-inland stations are flagged as out-of-domain.

**License:** Public domain (work of the U.S. Government, NOAA/NOS/CO-OPS).
