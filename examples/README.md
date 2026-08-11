# Examples

A small, committable sample input for VDatum Batch Transform. (The real working
dataset lives in `data/` and is gitignored.)

Provided in two formats (identical content) to show both are accepted:
`example_stations.csv` and `example_stations.ods` (sheet `Stations`).

## The sample stations

Five NJ back-bay stations chosen to exercise every code path (with the default
`tidal_datum = MLLW`, target `NAVD88`):

| Station ID | Name | Illustrates |
|---|---|---|
| 8531662 | Atlantic Highlands | No CO-OPS MLLW → `MLLW_Source = VDATUM_ZERO`; NAVD88 modeled by VDatum |
| 8531753 | Oceanic Bridge, Navesink River | MLLW from CO-OPS; NAVD88 from VDatum |
| 8531804 | Sea Bright | MLLW **and** NAVD88 from CO-OPS (plus internal QC note) |
| 8531833 | Red Bank, Navesink River | MLLW from CO-OPS; NAVD88 from VDatum |
| 8534739 | Dock Thorofare, Risley Channel | VDatum "Uncaught error" → exceptions + bug report |

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

Expected (when VDatum is healthy): **4 stations in results** — 1 with NAVD88
from CO-OPS (Sea Bright) and 3 modeled by VDatum — and **1 in exceptions**
(Dock Thorofare, the VDatum server-fault point, also listed in
`example_run_vdatum_bug_report.csv`). Because VDatum is intermittently cranky,
the exact split can vary run to run; use `--recheck-failures` to retry the
exceptions when the service recovers.
