# VDatum Batch Transform

Batch-transform a **tidal datum** to a **geodetic datum** (NAVD88) for a list of
NOAA CO-OPS tide stations, using two public NOAA API web services:

- **CO-OPS Tides & Currents (T&C)** — station-published datums via the
  [Metadata API](https://api.tidesandcurrents.noaa.gov/mdapi/prod/).
- **NOAA VDatum** — the
  [vertical datum transformation API](https://vdatum.noaa.gov/docs/services.html),
  used to model the geodetic value when CO-OPS doesn't publish it.

You pick one tidal datum (`MLLW`, `MLW`, `MHW`, `MHHW`, or `LMSL`) as the zero
plane; the tool reports NAVD88 relative to it. The tool is driven entirely by an
INI config file and writes CSV output. The input table is treated as
**read-only** and is never modified.

---

## Pipeline

One tidal datum in (the **zero plane**), one geodetic datum out (**NAVD88**),
per station (identified by its CO-OPS Station ID and lat/lon):

**1. Tidal datum (the zero plane).** INI-selectable: `MLLW`, `MLW`, `MHW`,
`MHHW`, or `LMSL`. Observed values come **only** from CO-OPS.
   - Present in CO-OPS → `ST_<TIDAL>` = observed value, `<TIDAL>_Source = COOPS`.
   - Absent → `ST_<TIDAL> = 0`, `<TIDAL>_Source = VDATUM_ZERO`, with a note: the
     tidal datum is being used as the 0 reference plane for the VDatum transform
     (it is *not* an observed value — observed tidal values are CO-OPS-only).

**2. Geodetic datum (NAVD88), relative to the tidal zero plane.**
   - CO-OPS publishes it → `ST_NAVD88 = NAVD88 − tidal` (both from CO-OPS),
     `NAVD88_Source = COOPS`.
   - Otherwise → **VDatum**: transform `(tidal_datum, s_z = 0) → NAVD88` at the
     station's lat/lon. `NAVD88_Source = VDATUM`; `VDatum_uncertainty` is
     VDatum's reported uncertainty for that transform.
     - Out of VDatum domain / persistent server fault → the station goes to the
       **exceptions** file (no NAVD88 obtainable).

A station lands in **exceptions** only when CO-OPS has no NAVD88 **and** VDatum
cannot transform the point. Note a station can have no observed tidal value
(`VDATUM_ZERO`) yet still obtain a valid modeled NAVD88 — it stays in results.

### The VDatum sign convention (important)

VDatum uses an **"up-is-negative"** convention: for a `(tidal = 0) → NAVD88`
transform it returns the NAVD88 height of the tidal-0 surface with up negative.
CO-OPS (and most users) express the geodetic value **relative to** the tidal
datum with **up-is-positive**. This tool therefore **sign-flips** VDatum's
value (`ST_NAVD88 = −t_z`) so VDatum- and CO-OPS-sourced values share one
convention in the same column.

Because the tidal datum *is* the 0 plane fed to VDatum, the input carries no
elevation uncertainty — all uncertainty is on the NAVD88 (output) side, which
is exactly the single `VDatum_uncertainty` VDatum reports.

See the last FAQ at <https://vdatum.noaa.gov/docs/faqs.html> for background on
the up-is-negative convention.

### Internal QC cross-check (advisory)

When **both** the tidal datum and NAVD88 come from CO-OPS, the tool can also
query VDatum for the modeled NAVD88 and compare (enabled by default,
`[qc] crosscheck_coops`). The CO-OPS observed value is **always retained**; the
check only adds a note, flagging when the VDatum modeled value differs by more
than VDatum's reported uncertainty (a likely VDatum grid anomaly at that point).

---

## Input format

The source table can be **`.csv`, `.ods`, or `.xlsx`** (auto-detected by file
extension; the sheet/tab name applies to `.ods`/`.xlsx` only). It is read-only —
the tool never modifies it.

**One file per run.** The tool processes only the single file named in
`config.ini` `[input] file`; it does not scan the input directory. To process
several sheets, run the tool once per file, each with its own `[output]
basename` so results don't overwrite each other.

**Required columns** (a header row is required). The default header names are:

| Purpose | Default header | config.ini key |
|---|---|---|
| Station ID (7-char CO-OPS id) | `Station ID` | `station_id_col` |
| Latitude (decimal degrees) | `Latitude` | `latitude_col` |
| Longitude (decimal degrees, °W negative) | `Longitude` | `longitude_col` |

If your file uses different header names, change the `*_col` values in
`config.ini` `[input]` to match — you do **not** have to rename your columns.

**Optional pass-through columns** (`passthrough_cols`, e.g. `Region`,
`Station Name`) are copied unchanged into the output. Any listed column that
isn't present is warned about and left blank.

Minimal CSV example:

```csv
Station ID,Latitude,Longitude,Region,Station Name
8531804,40.3650,-73.9750,NJ Coastal Back Bays,Sea Bright
8531753,40.3767,-74.0150,NJ Coastal Back Bays,"Oceanic Bridge, Navesink River"
```

If a required column is missing the tool stops with a clear message naming the
missing column and the `config.ini` key that controls it.

---

## Output columns

Two CSVs are written:

- `output/<basename>.csv` — **results**: stations that obtained NAVD88 (from
  CO-OPS or VDatum).
- `output/<basename>_exceptions.csv` — stations with **no** NAVD88 (CO-OPS
  didn't publish it *and* VDatum couldn't transform the point).

Both share the same columns:

| Column | Meaning |
|---|---|
| *(pass-through)* | e.g. `Region`, `Station Name` |
| `Station ID`, `Latitude`, `Longitude` | station identity |
| `ST_<TIDAL> (u)` | tidal datum value (the zero plane); `0` when `VDATUM_ZERO` |
| `<TIDAL>_Source` | `COOPS` (observed) or `VDATUM_ZERO` (no CO-OPS value; used as 0 plane) |
| `ST_NAVD88 (u)` | NAVD88 relative to the tidal zero plane |
| `NAVD88_Source` | `COOPS` or `VDATUM` |
| `VDatum_uncertainty (u)` | VDatum's reported uncertainty (blank when NAVD88 came from CO-OPS) |
| `Notes` | provenance / QC / exception detail |

`<TIDAL>` is whichever datum the INI selects (e.g. `ST_MLLW`, `MLLW_Source`).
`(u)` is `m` or `ft` per the configured units.

A persistent-VDatum-fault report (`output/<basename>_vdatum_bug_report.csv`)
and a full run log (`output/<basename>.log`) are also written (see below).

### Smart region fallthrough

When `region = auto`, the tool tries each region in `region_try_order`. It
distinguishes two kinds of per-region failure:

- **Geographic miss** — `-999999`, "Input Region is not correct!", wrong-coast
  frame requirements, unsupported-datum-for-region, etc. These just mean *that
  region doesn't apply to this point*, so the tool moves on quietly (logged at
  DEBUG, surfaced in the `Note` as `region N/A`). They do **not** count as
  server errors.
- **Server fault** — the generic "Uncaught error" message. This is retried with
  exponential backoff (`[api] vdatum_server_retries`) since it's sometimes
  transient; if it persists it's recorded as a real VDatum bug.

Only persistent server/transport faults mark a station `NO_GEODETIC` (into
exceptions) and count toward the "VDatum likely down" verdict.

### VDatum bug report

If any station hit a persistent VDatum server fault, the tool writes
`output/<basename>_vdatum_bug_report.csv` — one row per (station, failing region
attempt) with the station, coordinates, region/frame, the exact VDatum message,
and the **exact failing URL**. This is ready to forward to the NOAA VDatum
Program Support team so the server-side fault can be reproduced and fixed.

### Logfile

Every run writes a full-detail log to `output/<basename>.log`, capturing each
API request URL (region + frame + all params) and the raw response. The console
shows a per-station summary; the logfile is the reproducible record — you can
copy a failing VDatum URL straight out of it.

### VDatum "is it down?" detection

**The VDatum API is intermittently cranky** — it returns HTTP 500s, timeouts,
and a generic `"Uncaught error, please contact NOAA VDatum Program Support
team."` message often enough that a batch run needs to tell a *service outage*
apart from stations that are *legitimately out of the tidal domain*.

To do this the tool runs a **canary**: a transform of a known-good point (from
VDatum's own API docs) that should always succeed when the service is healthy.

- **Startup** — canary runs before processing. If it fails, you get an
  immediate heads-up that VDatum may be down (T&C-sourced `OK` rows are still
  valid).
- **On the first VDatum server error** — the canary re-runs to attribute the
  cause: canary UP ⇒ the failure is point-specific; canary DOWN ⇒ the service
  itself looks unhealthy.
- **End of run** — if any VDatum server errors occurred, the canary runs once
  more to label the run.

**"Likely down" verdict:** if the fraction of VDatum calls that returned a
*server/API error* (not clean out-of-domain) meets/exceeds `down_error_rate`
(default **0.90**) **and** a canary check failed, the tool prints a loud
`*** VDatum API is LIKELY DOWN / UNHEALTHY ***` banner and exits with code **3**
(so cron/scripts can detect it). Output files are still written; re-run later to
fill the VDatum fallbacks. If some calls errored but the canary succeeded, the
tool instead notes the failures look point-specific — not a full outage.

The canary point and threshold are configurable in `[health]`; set
`canary_enabled = false` to disable the checks entirely.


---

## Setup

```bash
conda env create -f environment.yml
conda activate vdatum_batch_xform
```

## Usage

```bash
# Edit config.ini to point at your data and set datums / frames, then:
python vdatum_batch_transform.py --config config.ini

# Test on the first N stations:
python vdatum_batch_transform.py --config config.ini --limit 5

# Re-run ONLY the stations that ended up in exceptions with a VDatum
# server/API error (e.g. after VDatum recovers), merging successes into
# the same output:
python vdatum_batch_transform.py --config config.ini --recheck-failures
```

All configuration lives in `config.ini` (below). The command line is
intentionally minimal — just `--config`, `--limit`, and `--recheck-failures`.

## Configuration (`config.ini`)

Key options (see the file for the full annotated set):

- `[input]` — source `.csv`/`.ods`/`.xlsx` file, sheet (for spreadsheets), and
  the required/optional column names (see **Input format** above).
- `[datums]` — `tidal_datum` (the single zero-plane: `MLLW`, `MLW`, `MHW`,
  `MHHW`, or `LMSL`) and `geodetic_datum` (the transform target, e.g. `NAVD88`).
- `[vdatum]` — `s_h_frame` (input horizontal reference frame),
  `epoch_in`/`epoch_out`, `geoid`, and region handling. The VDatum source
  vertical frame is the configured `tidal_datum`; the target is the
  `geodetic_datum`. `region = auto` tries each region in `region_try_order`
  until one succeeds. Some regional tidal grids require a specific input frame
  — use the `region:FRAME` syntax (e.g. `chesapeak_delaware:IGS14`).
- `[api]` — `application` identifier for CO-OPS logs, throttle sleep, timeout,
  retries, and `vdatum_server_retries` (backoff retries for the transient
  "Uncaught error" fault).
- `[qc]` — `crosscheck_coops` (advisory internal QC; see Pipeline).
- `[health]` — canary health-check point, on/off toggle (`canary_enabled`), and
  the `down_error_rate` threshold for the "VDatum likely down" verdict.

---

## Notes / caveats

- CO-OPS **throttles** heavy API use; a courtesy sleep is applied between
  calls (`[api] sleep_between_calls`).
- VDatum tidal transforms only extend a short distance inland of the MHW
  shoreline; far-inland stations return `-999999` and land in exceptions.
- **VDatum is intermittently unreliable** (500s / timeouts / "Uncaught error").
  The canary check (see above) distinguishes an outage from bad-point failures;
  if VDatum looks down, re-run later (or `--recheck-failures`) — the
  CO-OPS-sourced rows are unaffected.
- Observed tidal datum values come from CO-OPS only. When CO-OPS lacks the
  tidal datum, it is used as the 0 reference plane for the VDatum transform
  (`ST_<TIDAL> = 0`, source `VDATUM_ZERO`) — that is not an observed value.

### Exit codes

- `0` — completed normally.
- `3` — completed, but VDatum was judged **likely down** (high server-error
  rate + failed canary). Output files are still written.


---

## Provenance / license

Developed at NOAA / NOS / CO-OPS. As a work of the U.S. Government, this code
is in the **public domain** (17 U.S.C. § 105). Provided "as is" without
warranty of any kind.
