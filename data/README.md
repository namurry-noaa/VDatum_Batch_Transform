# `data/` — input directory

Put your source table here (or anywhere — the path is set in `config.ini`
`[input] file`).

Accepted formats: **`.csv`, `.ods`, `.xlsx`**.

**One file per run.** The tool processes exactly the single file named in
`config.ini [input] file`; it does not scan this directory. To process several
sheets, run the tool once per file — give each run its own `[output] basename`
so the results don't overwrite each other.

Required columns (a header row is required; names configurable in
`config.ini [input]`):

| Purpose | Default header | config key |
|---|---|---|
| Station ID (7-char CO-OPS id) | `Station ID` | `station_id_col` |
| Latitude (decimal degrees) | `Latitude` | `latitude_col` |
| Longitude (decimal degrees, °W negative) | `Longitude` | `longitude_col` |

Optional descriptive columns (e.g. `Region`, `Station Name`) can be carried
through to the output via `passthrough_cols`.

A ready-to-run sample lives in `examples/`. This `data/` directory is otherwise
gitignored, so your working datasets stay local and are never committed.
