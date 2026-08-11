# Examples

A small, committable sample input for VDatum Batch Transform. (The real working
dataset lives in `data/` and is gitignored.)

Provided in two formats (identical content) to show both are accepted:
`example_stations.csv` and `example_stations.ods` (sheet `Stations`).

## The sample stations

Five NJ back-bay stations chosen to exercise every code path:

| Station ID | Name | Illustrates |
|---|---|---|
| 8531662 | Atlantic Highlands | `NO_TIDAL` (no published datums in CO-OPS) |
| 8531753 | Oceanic Bridge, Navesink River | `VDATUM_FALLBACK` (MLLW in T&C, NAVD88 via VDatum) |
| 8531804 | Sea Bright | `OK` (NAVD88 published in T&C) |
| 8531833 | Red Bank, Navesink River | `VDATUM_FALLBACK` |
| 8534739 | Dock Thorofare, Risley Channel | `VDATUM_API_ERROR` (persistent VDatum "Uncaught error"; appears in the bug report) |

Columns: `Region, Station Name, Station ID, Latitude, Longitude`
(the `.ods` uses sheet name `Stations`).

### Run it

Point `config.ini` at either file (or copy it to a scratch config), e.g. for
the CSV:

```ini
[input]
file  = examples/example_stations.csv

[output]
dir      = examples/output
basename = example_run
```

or the ODS (add the sheet name):

```ini
[input]
file  = examples/example_stations.ods
sheet = Stations
```

then:

```bash
python vdatum_batch_transform.py --config config.ini
```

Expected: 1 `OK`, 2 `VDATUM_FALLBACK`, 1 `NO_TIDAL`, and 1 `VDATUM_API_ERROR`
(the last also written to `example_run_vdatum_bug_report.csv`).
