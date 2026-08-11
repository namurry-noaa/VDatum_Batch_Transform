# `output/` — results directory

Generated files land here (path set in `config.ini` `[output] dir`,
base name in `[output] basename`). For a run with basename `NAME`:

| File | Contents |
|---|---|
| `NAME.csv` | All stations with computed columns |
| `NAME_exceptions.csv` | Only rows needing review (non-`OK`) |
| `NAME_vdatum_bug_report.csv` | Persistent VDatum server faults + exact failing URLs (only if any occurred) |
| `NAME.log` | Full per-run request/response log |

Everything in this directory except this README is gitignored, so results are
never committed. The directory is shipped (with this README) so you don't have
to create it before the first run.
