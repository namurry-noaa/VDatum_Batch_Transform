# VDatum Batch Transform

Batch-transform **tidal datums** to a **geodetic datum** for a list of NOAA
CO-OPS tide stations, using two public NOAA API web services:

- **CO-OPS Tides & Currents (T&C)** — station-published datums via the
  [Metadata API](https://api.tidesandcurrents.noaa.gov/mdapi/prod/).
- **NOAA VDatum** — the
  [vertical datum transformation API](https://vdatum.noaa.gov/docs/services.html),
  used as a fallback when a station has a tidal datum but no published
  geodetic datum.

The tool is driven entirely by an INI config file and writes CSV output.
The input spreadsheet is treated as **read-only** and is never modified.

---

## Pipeline

For each station (identified by its 7-digit CO-OPS Station ID and lat/lon):

1. **Pull published datums** from the CO-OPS Metadata API.
2. **Tidal (zero-plane) datum** — e.g. `MLLW`, relative to Station Datum (STND):
   - If **absent** → value is `NULL`, and processing of that station **halts**
     (there is no tidal reference to convert). Status `NO_TIDAL`.
3. **Geodetic datum** — e.g. `NAVD88`, expressed relative to the tidal
   zero-plane (`geodetic(STND) - tidal(STND)`):
   - If **present in T&C** → store it. Source `COOPS`, status `OK`.
   - If **absent** → fall back to **VDatum**: transform
     `(tidal_datum, z = 0)` at the station lat/lon to the geodetic datum.
     - Success → store the value. Source `VDATUM`, status `VDATUM_FALLBACK`.
     - Outside VDatum's transformation domain (`-999999` / region error) →
       value is `NULL`, processing continues. Status `VDATUM_FAIL`.

### The VDatum sign convention (important)

VDatum uses an **"up-is-negative"** convention: it returns the geodetic height
of the *tidal = 0* surface. T&C (and most users) expect the geodetic value
**relative to** the tidal datum with **up-is-positive**. This tool records
**both**:

| Column                 | Meaning                                                        |
|------------------------|----------------------------------------------------------------|
| `VDatum_raw`           | Raw VDatum `t_z` (up-is-negative, VDatum native convention)    |
| `ST_<GEODETIC>_VDatum` | Sign-flipped to T&C convention (up-is-positive, `= -t_z`)      |

See the last FAQ at <https://vdatum.noaa.gov/docs/faqs.html> for background.

---

## Input format

The source table can be **`.csv`, `.ods`, or `.xlsx`** (auto-detected by file
extension; the sheet/tab name applies to `.ods`/`.xlsx` only). It is read-only —
the tool never modifies it.

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

`output/<basename>.csv` (all stations) and
`output/<basename>_exceptions.csv` (only rows needing review):

- pass-through columns (e.g. `Region`, `Station Name`)
- `Station ID`, `Latitude`, `Longitude`
- `ST_<TIDAL> (u)` — tidal datum relative to STND

Then, **for each geodetic target** in `geodetic_datums`, a column block:

- `ST_<TARGET> (u)` — geodetic relative to tidal, from **T&C**
- `ST_<TARGET>_VDatum (u)` — geodetic relative to tidal, from **VDatum** (T&C sign)
- `<TARGET>_VDatum_raw (u)` — raw VDatum value (up-is-negative)
- `<TARGET>_VDatum_uncertainty (u)`, `<TARGET>_VDatum_region`
- `<TARGET>_Geodetic_source` — `COOPS` | `VDATUM`
- `<TARGET>_Status` — per-target status (see below)
- `<TARGET>_Note` — per-target detail

Finally two station-level columns:

- `Station_status` — `PROCESSED` | `NO_TIDAL` | `COOPS_ERROR`
- `Station_note`

Per-target `Status` values:
  - `OK` — geodetic value came straight from CO-OPS T&C.
  - `VDATUM_FALLBACK` — geodetic value filled via VDatum.
  - `VDATUM_OUT_OF_DOMAIN` — point is outside VDatum's tidal grid for every
    *applicable* region (honest `-999999`, or a definitive geographic rejection
    such as "Input Region is not correct!"); value `NULL`, continued.
  - `VDATUM_API_ERROR` — VDatum returned a *persistent* server fault (the
    "Uncaught error, please contact NOAA VDatum Program Support team." message,
    retried with backoff) or an HTTP/transport error; value `NULL`, continued.
    These are captured in a separate bug-report file (below).

Station-level `NO_TIDAL` means the station has no published tidal datum, so no
targets are attempted; `COOPS_ERROR` means the CO-OPS datums request failed.

The per-target `Note` lists each region/frame attempt: `region N/A (...)` for a
region that doesn't cover the point, vs `SERVER fault (...)` for a genuine
VDatum server error.

(`u` = `m` or `ft` per the configured units.)

### Multiple geodetic targets

Set `geodetic_datums` in `[datums]` to a comma-separated list (e.g.
`NAVD88` or `NAVD88, MHW`). One tidal zero-plane (`tidal_datum`) is shared; each
target gets its own column block and its own T&C-then-VDatum resolution.
(NAPGD2022 is not yet on the VDatum API — a separate extractor handles it.)

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

Only persistent server/transport faults mark a target `VDATUM_API_ERROR` and
count toward the "VDatum likely down" verdict.

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

# Re-run ONLY the targets that previously failed with VDATUM_API_ERROR
# (e.g. after VDatum recovers), merging successes into the same output:
python vdatum_batch_transform.py --config config.ini --recheck-failures
```

All configuration lives in `config.ini` (below). The command line is
intentionally minimal — just `--config`, `--limit`, and `--recheck-failures`.

## Configuration (`config.ini`)

Key options (see the file for the full annotated set):

- `[input]` — source `.csv`/`.ods`/`.xlsx` file, sheet (for spreadsheets), and
  the required/optional column names (see **Input format** above).
- `[datums]` — `tidal_datum` (single zero-plane) and `geodetic_datums`
  (comma-separated target list), as CO-OPS abbreviations. (The legacy
  singular `geodetic_datum` key is still accepted.)
- `[vdatum]` — `s_h_frame` (input horizontal reference frame),
  `epoch_in`/`epoch_out`, `s_v_frame`, `geoid`, and region handling. The VDatum
  target vertical frame is taken per target from `geodetic_datums`.
  `region = auto` tries each region in `region_try_order` until one succeeds.
  Some regional tidal grids require a specific input frame — use the
  `region:FRAME` syntax (e.g. `chesapeak_delaware:IGS14`).
- `[api]` — `application` identifier for CO-OPS logs, throttle sleep, timeout,
  retries, and `vdatum_server_retries` (backoff retries for the transient
  "Uncaught error" fault).
- `[health]` — canary health-check point, on/off toggle (`canary_enabled`), and
  the `down_error_rate` threshold for the "VDatum likely down" verdict.

---

## Notes / caveats

- CO-OPS **throttles** heavy API use; a courtesy sleep is applied between
  calls (`[api] sleep_between_calls`).
- VDatum tidal transforms only extend a short distance inland of the MHW
  shoreline; far-inland stations return `-999999` (`VDATUM_OUT_OF_DOMAIN`).
- **VDatum is intermittently unreliable** (500s / timeouts / "Uncaught error").
  The canary check (see above) distinguishes an outage from bad-point failures;
  if VDatum looks down, re-run later — the CO-OPS-sourced rows are unaffected.
- Datum values from CO-OPS are relative to **Station Datum (STND)**; this tool
  re-references the geodetic datum to the tidal zero-plane for you.

### Exit codes

- `0` — completed normally.
- `3` — completed, but VDatum was judged **likely down** (high server-error
  rate + failed canary). Output files are still written.


---

## Provenance / license

Developed at NOAA / NOS / CO-OPS. As a work of the U.S. Government, this code
is in the **public domain** (17 U.S.C. § 105). Provided "as is" without
warranty of any kind.
