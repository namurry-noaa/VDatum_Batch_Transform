# VDatum Batch Transform

Batch-transform a **tidal datum** to a **geodetic datum** (NAVD88) for a list of
NOAA CO-OPS tide stations, using two public NOAA API web services:

- **CO-OPS (Tides & Currents)** — station-published datums via the
  [Metadata API](https://api.tidesandcurrents.noaa.gov/mdapi/prod/).
- **NOAA VDatum** — the
  [Vertical Datum Transformation API](https://vdatum.noaa.gov/docs/services.html),
  used to transform between different tidal and geodetic datums, and also returns
  a geodetic value when CO-OPS is not able to publish one as part of their normal roster of tidal datum products. Additional information on these services can be found at their respective websites.

In this initial version, the user chooses one of the following tidal datums
(`MLLW`, `MLW`, `MHW`, `MHHW`, or `LMSL`), which then serves as the 'zero
plane' for the batch transformation. This utility then fetches an NAVD88 value relative to that tidal datum. The utility is driven entirely by an INI config file and writes CSV output. The input table is treated as **read-only** and is never modified.

---

## Pipeline

One tidal datum is chosen (the **zero plane**), one geodetic datum out (**NAVD88**),
per station (identified by its CO-OPS Station ID and lat/lon):

**1. Tidal datum computed values from observations.** INI-selectable: `MLLW`, `MLW`, `MHW`, `MHHW`, or `LMSL`. Computed values are pulled in via the CO-OPS API from the individual CO-OPS tide station pages under 'Datums'.
   - If a value for the chosen tidal datum is present via the CO-OPS API, `ST_<TIDAL>` = the computed value for the datum chosen.
   - If it is not, `ST_<TIDAL>` is then set to 0, and NAVD88 is instead derived via the VDatum API. That is, the tidal datum being used as the 0 reference plane for the VDatum transform is not a computed value; it only serves as the zero plane for computing the modeled NAVD88 value at that station's coordinates.

**2. Geodetic datum (NAVD88) for the tide station, if possible from observations:**
   - If CO-OPS publishes a computed NAVD88 value for the station in question, then `ST_NAVD88 = NAVD88 − tidal datum value` (the tidal datum selected to transform from) and `NAVD88_Source = COOPS`.
   - Otherwise → **VDatum**: transform `(tidal_datum, s_z = 0) → NAVD88` at the
     station's lat/lon. `NAVD88_Source = VDATUM`; `VDatum_uncertainty` is
     VDatum's reported uncertainty for the transform at that station's coordinates.
   - If the coordinate set is outside of the VDatum transformation domain, or if API server faults are returned, the station goes to the **exceptions** file (no NAVD88 obtainable).

Therefore, a station lands in **exceptions** only when CO-OPS has no NAVD88 **and** VDatum cannot obtain a transformation of that point to a modeled NAVD88 value. Note that a station can have no computed tidal value (`VDATUM_ZERO`) yet still obtain a valid modeled NAVD88. This is due to the tidal datum value becoming the zero plane for an attempted VDatum transformation.

### The VDatum sign convention (important)

VDatum uses a **"negative ascending depth"** convention. That is, for a `given tidal datum value of zero transformed to NAVD88`, the API returns a negative value if the elevation is above the tidal datum, and a positive value if the elevation is below the tidal datum. CO-OPS (and most users) express the geodetic value **relative to** the tidal datum with a **positive ascending depth**. Therefore, when a VDatum transform is required, this utility **inverts** VDatum's value (`ST_NAVD88 = −t_z`) so the result shares the same sign convention as the CO-OPS datum values.

Because the tidal datum *is* the 0 plane fed to VDatum, the input carries no
elevation uncertainty — all uncertainty is on the NAVD88 output side, which
is exactly the single `VDatum_uncertainty` VDatum reports when it returns a transformation value.

For more information on VDatum's sign convention behavior, please see the last FAQ at <https://vdatum.noaa.gov/docs/faqs.html>.

### Internal QC cross-check (advisory)

When **both** the tidal datum and NAVD88 values are returned from the CO-OPS API, this tool also can query VDatum for the modeled NAVD88 at the same location, and compare them as a 'verification' of sorts. This is enabled by default in the INI (`[qc] crosscheck_coops`). The CO-OPS computed value is of course always retained; the
check only adds a note, flagging when the VDatum modeled value differs by more
than VDatum's reported uncertainty. In such a case, the cause is very likely a VDatum grid anomaly, though some further investigation by the user would be worthwhile.

---

## Input format

The source table can be a **`.csv`, `.ods`, or `.xlsx`** (auto-detected by file
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

If the input file uses different header names, change the `*_col` values in
`config.ini` `[input]` to match — the user does **not** have to rename columns in the actual data file.

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
  transient; if it persists it is considered a real VDatum grid issue, and recorded as such.

Only persistent server/transport faults mark a station `NO_GEODETIC` (into
exceptions) and count toward the "VDatum likely down" verdict.

### VDatum bug report

If any station hits a persistent VDatum server fault, the tool writes
`output/<basename>_vdatum_bug_report.csv` — one row per (station, failing region
attempt) with the station, coordinates, region/frame, the exact VDatum message,
and the **exact failing URL**. This is ready to forward to the NOAA VDatum Support team for further examination.

### Logfile

Every run writes a full-detail log to `output/<basename>.log`, capturing each
API request URL (region + frame + all params) and the raw response. The console
shows a per-station summary; the logfile is the reproducible record — the user can
copy a failing VDatum URL directly from the log file.

### VDatum API outage detection

**The VDatum API can be unresponsive at times** — that is, it returns HTTP 500s, timeouts, and/or a generic message: `"Uncaught error, please contact NOAA VDatum Program Support team."` If returned often enough, a batch run needs to tell a *service outage* apart from stations that are *legitimately out of the tidal domain*.

To do this, the tool runs a **"canary"**: a transform of a known-good point from
VDatum's own API docs that should always succeed when the service is healthy.

- **Startup** — canary runs before processing. If it fails, you get an
  immediate heads-up that VDatum may be down (T&C-sourced `OK` rows are still
  valid).
- **On the first VDatum server error** — the canary re-runs to attribute the
  cause: canary UP ⇒ the failure is point-specific; canary DOWN ⇒ the service
  itself looks unhealthy.
- **End of run** — if any VDatum server errors occurred, the canary runs once
  more to label the run.

**Outage verdict:** if the fraction of VDatum calls that returned a
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
- Computed tidal datum values come from CO-OPS only. When CO-OPS lacks the
  tidal datum, it is used as the 0 reference plane for the VDatum transform
  (`ST_<TIDAL> = 0`, source `VDATUM_ZERO`). This is not then considered for use as the actual tidal datum, as it was not CO-OPS computed.
  
### Exit codes

- `0` — completed normally.
- `3` — completed, but VDatum was judged **likely down** (high server-error
  rate + failed canary). Output files are still written.


---

## Provenance / license

Developed at NOAA / NOS / CO-OPS. As a work of the U.S. Government, this code
is in the **public domain** (17 U.S.C. § 105). Provided "as is" without
warranty of any kind.
