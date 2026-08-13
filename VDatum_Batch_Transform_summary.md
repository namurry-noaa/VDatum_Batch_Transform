================================================================================
VDATUM_BATCH_TRANSFORM — PROJECT SUMMARY  (VDatum_Batch_Transform_summary.md)
Nate Murry — NOAA / NOS / CO-OPS
Location: ~/NOAA/Coding/Git/Local_Repos/VDatum_Batch_Transform
Last Updated: 2026-08-12
Note: formerly a local, gitignored *_summary.claude reminder file; now a
      tracked, committed project summary (.md). Content is being formalized
      over time; the plain-text layout below is legacy and may be restructured.
================================================================================

PURPOSE
  Batch-transform tidal datums -> a geodetic datum for a sheet of CO-OPS tide
  stations. Reusable, INI-configured Python tool. Public-domain (NOAA) — meant
  to also be usable by people outside NOAA. CSV output only (ODS write-back was
  considered then dropped).

********************************************************************************
*** v2.0.0 REDESIGN (2026-08-12) — the sections below marked "(v1)" describe   *
*** the OLD pipeline/schema. The CURRENT design is in the "v2.0.0" block near  *
*** the bottom. Read that block first; it supersedes the v1 pipeline/columns.  *
********************************************************************************

--------------------------------------------------------------------------------
PIPELINE (per station)
--------------------------------------------------------------------------------
  1. Pull published datums from CO-OPS Metadata API (MDAPI):
       https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/<ID>/datums.json?units=metric
     (Note: the DATA API 'datums' product is retired; MDAPI is the source.)
  2. Tidal (zero-plane) datum, default MLLW (rel. to STND):
       - absent -> NULL, HALT station. Status NO_TIDAL.
  3. Geodetic datum, default NAVD88, expressed relative to the tidal zero-plane
     (geodetic(STND) - tidal(STND)):
       - present in T&C -> store. Source COOPS, status OK.
       - absent -> VDatum fallback API:
           https://vdatum.noaa.gov/vdatumweb/api/convert
           transform (tidal_datum, z=0) at lat/lon -> geodetic.
           success -> store. Source VDATUM, status VDATUM_FALLBACK.
           -999999 / region error -> NULL, continue. Status VDATUM_FAIL.

  VDATUM SIGN "ISM": VDatum is up-is-negative (returns geodetic height of the
  tidal=0 surface). T&C is up-is-positive. We store BOTH:
       VDatum_raw            = raw t_z (up negative)
       ST_<GEO>_VDatum       = -t_z  (T&C convention, up positive)
  See last FAQ: https://vdatum.noaa.gov/docs/faqs.html

--------------------------------------------------------------------------------
FILES
--------------------------------------------------------------------------------
  vdatum_batch_transform.py  main tool (argparse: --config, --limit)
  config.ini                 all knobs (input file/cols, datums, VDatum frames,
                             epochs, region auto-try list, API throttle)
  environment.yml            conda env spec
  README.md                  full docs
  LICENSE                    public domain / CC0
  .gitignore                 ignores output/, __pycache__, *_summary.claude, etc.
  data/PJH_Points.ods        SOURCE (read-only). Sheet 'All Stations', 107 rows.
  data/PJH_Pts.xlsx          xlsx twin of the source.
  output/                    generated CSVs (gitignored):
      PJH_Points_transformed.csv          all 107 stations
      PJH_Points_transformed_exceptions.csv   non-OK rows

--------------------------------------------------------------------------------
ENVIRONMENT
--------------------------------------------------------------------------------
  conda env: vdatum_batch_xform  (Python 3.12; pandas, requests, odfpy)
  Build: conda env create -f environment.yml
  Run:   conda activate vdatum_batch_xform
         python vdatum_batch_transform.py --config config.ini [--limit N]

--------------------------------------------------------------------------------
CONFIG NOTES LEARNED FROM THE APIs
--------------------------------------------------------------------------------
  - Horizontal ref / input datum / output datum are all INI-configurable
    (per user request). Default s_h_frame=NAD83_2011, epoch 2010.0.
  - region=auto tries region_try_order until one succeeds. Some regional tidal
    grids REQUIRE a specific input horizontal frame -> use "region:FRAME"
    syntax. Discovered: chesapeak_delaware requires IGS14 for tidal transforms
    (contiguous uses NAD83_2011). Encoded as "chesapeak_delaware:IGS14".
  - VDatum -999999 = out of domain (inland / masked). Tidal grids only reach a
    km or two inland of MHW, so far-inland stations legitimately fail.

--------------------------------------------------------------------------------
VDATUM CRANKINESS / "IS IT DOWN?" DETECTION  (added 2026-08-11)
--------------------------------------------------------------------------------
  The VDatum API is known to be intermittently unreliable (HTTP 500s, timeouts,
  generic "Uncaught error, please contact NOAA VDatum Program Support team.").
  We must distinguish a SERVICE OUTAGE from stations legitimately out of domain.

  MECHANISM — a "canary" transform of a known-good point (VDatum docs example,
  -75.211,36.129 -> MLLW) that should always succeed if the service is healthy:
    * startup canary            -> heads-up before processing
    * canary on FIRST API error -> attribute cause (up=point-specific;
                                    down=service unhealthy)
    * end-of-run canary         -> label the run (only if errors occurred)

  VERDICT: if (server/API-error rate >= down_error_rate, default 0.90) AND a
  canary failed -> loud "*** VDatum API is LIKELY DOWN / UNHEALTHY ***" banner
  + exit code 3 (cron/scripts can detect). Output files still written; re-run
  later to fill fallbacks. If errors occurred but canary is UP -> notes the
  failures look point-specific, not an outage.

  Tracking: run() counts vd_calls / vd_errors (API_ERROR) / vd_domain
  (OUT_OF_DOMAIN); process_station() returns an outcome tag for this.
  Config: [health] section — canary_enabled, canary point/frames,
  down_error_rate. Console warnings are terse (first region only); the full
  multi-region detail stays in the DEBUG logfile and the CSV Note field.

  TESTED: simulated outage (VDATUM_URL -> unroutable 10.255.255.1) correctly
  fired the DOWN banner + exit 3 at 100% error rate; live full run shows 38%
  error rate with canary UP -> point-specific note (correct, not an outage).

--------------------------------------------------------------------------------
SESSION 2026-08-12 (morning) — EXCEPTION HANDLING REFINED
--------------------------------------------------------------------------------
  Reviewed the 107-station exception output with user. Findings + changes:

  1. SMART REGION FALLTHROUGH (was: try all regions, log every rejection as an
     "error"). Now _classify_vdatum_message() buckets each per-region failure:
       GEOGRAPHIC -> "Input Region is not correct!", "should be IGS14 for
                     Tidal", "only cover ...", "Unsupported vertical datum",
                     and clean -999999. Means "this region doesn't apply here";
                     surfaced in Note as "region N/A (...)", NOT an error.
       SERVER     -> the generic "Uncaught error, please contact NOAA VDatum
                     Program Support team." fault.
     Only SERVER/transport faults count toward VDATUM_API_ERROR + the DOWN
     verdict. This de-noised the Note field (no more misleading hi/as/gcnmi
     "errors" for NJ points) and makes OUT_OF_DOMAIN vs API_ERROR accurate.

  2. RETRY-THEN-REPORT for the SERVER fault. [api] vdatum_server_retries
     (default 3) retries ONLY the "Uncaught error" with exponential backoff
     (sometimes transient). Confirmed via direct curl x3 that our 8 failing
     points are PERSISTENT (not transient) -> genuine VDatum server bug.

  3. VDATUM BUG REPORT file: output/<basename>_vdatum_bug_report.csv, one row
     per (station, failing region attempt) with station/coords/region/frame/
     exact VDatum message/EXACT FAILING URL. Ready to forward to the NOAA
     VDatum Program Support team. write_vdatum_bug_report() only writes it if
     there are server faults.

  RE-RUN (2026-08-12): OK 11, VDATUM_FALLBACK 16, VDATUM_API_ERROR 10,
  NO_TIDAL 70; 26 VDatum calls, 38% server errors, canary UP -> point-specific
  note (correct). Bug report: 18 rows across 10 unique stations (each fails in
  both contiguous & chesapeak_delaware:IGS14; other regions are geographic N/A).
  NOTE: the count "10" API_ERROR includes the 2 that curl-tested as -999999 in
  contiguous but still hit SERVER faults in chesapeak_delaware -> correctly
  classified API_ERROR because a real server fault occurred on an applicable
  region. All 10 are Great Egg Harbor / Mullica / upper Delaware R points.
  ACTION ITEM (user): send the bug report to VDatum support.

  New config knob: [api] vdatum_server_retries = 3.
  New status vocabulary unchanged (OUT_OF_DOMAIN / API_ERROR) but semantics
  tightened per above.

--------------------------------------------------------------------------------
SESSION 2026-08-12 (cont.) — BASE FEATURES: multi-target, CLI, recheck
--------------------------------------------------------------------------------
  Three "base" features added before git (user decision):

  1. MULTIPLE GEODETIC TARGETS (one tidal zero-plane, many targets).
     [datums] geodetic_datums = NAVD88            (comma list, e.g. NAVD88, MHW)
     - Legacy singular 'geodetic_datum' still accepted as fallback.
     - Data model refactor: StationRow now has st_tidal + a station-level
       status (PROCESSED / NO_TIDAL / COOPS_ERROR) + a dict of TargetResult
       (one per geodetic target, each with its own status/source/values/faults).
     - vdatum_convert() now takes t_v_frame arg (per target).
     - CSV: base cols (passthrough, ID, lat/lon, ST_<TIDAL>) then a per-target
       BLOCK: ST_<T>, ST_<T>_VDatum, <T>_VDatum_raw, <T>_VDatum_uncertainty,
       <T>_VDatum_region, <T>_Geodetic_source, <T>_Status, <T>_Note; then
       Station_status, Station_note.
     - Summary now tallies per-target status ("NAVD88:OK", "MHW:OK", ...).
     - NAPGD2022 deliberately OUT OF SCOPE for now (not on VDatum API; separate
       extractor exists in another repo).

  2. CLI OVERRIDES (flags override config.ini for one-offs):
     --input --sheet --tidal-datum --geodetic-datums --units --region
     --out-dir --basename.  apply_overrides() layers them onto the loaded Config.

  3. --recheck-failures MODE (merge into existing output):
     Loads prior <basename>.csv, reconstructs StationRow/TargetResult from it
     (_load_prior_results / _rows_from_prior), re-runs ONLY the (station,target)
     pairs whose prior <T>_Status == VDATUM_API_ERROR, and rewrites the FULL
     merged results/exceptions/bug-report. Needs a prior results file (errors
     out otherwise). Use when VDatum recovers.

  TESTED (2026-08-12, VDatum up):
   - multi-target NAVD88,MHW on --limit 6: MHW came from COOPS (OK) where NAVD88
     needed VDatum fallback -> per-target columns correct.
   - CLI overrides (--geodetic-datums/--basename/--out-dir) honored.
   - full run: NAVD88 -> OK 11 / VDATUM_FALLBACK 16 / VDATUM_API_ERROR 10 /
     NO_TIDAL 70 (unchanged, as expected).
   - --recheck-failures: 10 API_ERROR retried (still 100% err this session, real
     VDatum bug), 107 rows preserved, prior FALLBACK values intact. Merge OK.

  run() signature: run(cfg, limit=None, recheck_failures=False).
  Docs (README, config.ini) updated for all three features.

--------------------------------------------------------------------------------
SESSION 2026-08-12 (cont.) — SCOPE PRUNE: removed CLI overrides
--------------------------------------------------------------------------------
  User self-check on feature creep. Decision after honest review:
   - REMOVED the CLI override flags (--input/--sheet/--tidal-datum/
     --geodetic-datums/--units/--region/--out-dir/--basename) and
     apply_overrides(). They just duplicated the INI for a tool run a handful
     of times; classic gold-plating. CLI is now minimal: --config, --limit,
     --recheck-failures.
   - REVERTED the .csv reader in read_input() (it existed only to prop up the
     CLI-driven example). Back to .ods/.xlsx only.
   - KEPT multiple geodetic targets (geodetic_datums list): user wants it since
     not everyone starts at MLLW / may want the other core datums. Guardrail:
     do NOT recreate a "VDatum lite" — just make batch transforms easier.
   - Example input is now examples/example_stations.ods (sheet 'Stations'),
     5 stations covering every path; run via a config pointed at it (README).

  Philosophy note recorded: features that were DATA/API-driven (recheck, canary,
  retry, bug report, logging) were kept; the convenience-only CLI layer was cut.

--------------------------------------------------------------------------------
SESSION 2026-08-12 (cont.) — INPUT FORMATS + COLUMN DOCS
--------------------------------------------------------------------------------
  - read_input() now accepts .csv, .ods, .xlsx (ext auto-detect); clear
    ValueError on any other extension. CSV ignores the [input] sheet key.
  - FRIENDLY VALIDATION: missing required column(s) -> readable error naming
    each missing header AND the config.ini [input] key that controls it, plus
    the columns actually found. Missing pass-through cols -> warning, left
    blank (non-fatal). main() catches ValueError/KeyError -> exit code 2.
  - config.ini [input] rewritten to document required (station_id_col,
    latitude_col, longitude_col) vs optional (passthrough_cols) headers and the
    accepted formats.
  - README: new "Input format" section (formats, required/optional column
    table mapped to config keys, minimal CSV example, missing-col behavior).
  - examples/: added example_stations.csv (twin of the .ods); examples/README
    shows running either. Both verified end-to-end (1 OK / 2 FALLBACK /
    1 NO_TIDAL / 1 API_ERROR). Missing-column error path tested (exit 2).

  ANSWER to "is ODS the only input?": no — CSV + ODS + XLSX all supported;
  CSV is the simplest drop-in for a batch run (just needs the header row with
  Station ID / Latitude / Longitude, or matching config *_col names).

--------------------------------------------------------------------------------
GIT: COMMITTED & PUSHED (2026-08-12)
--------------------------------------------------------------------------------
  User ran init/remote/add/commit/push themselves (learning git). Live on
  GitHub: github.com:namurry-noaa/VDatum_Batch_Transform (main, tracking
  origin/main, working tree clean).
  Tracked (14, as of v1.0.1): .gitattributes .gitignore LICENSE README.md
    CHANGELOG.md RELEASE_NOTES.md config.ini environment.yml
    vdatum_batch_transform.py data/README.md output/README.md
    examples/README.md examples/example_stations.csv
    examples/example_stations.ods
  Ignored (correct): data/* except data/README.md (real PJH sheets),
    output/* except output/README.md, output_*/, __pycache__, *_summary.claude.

  >>> GOING FORWARD: assistant does NOT run git. Assistant prepares/stages-in-
      spirit (edits files) and, when asked, prints the exact command sequence;
      USER executes all git.

--------------------------------------------------------------------------------
RELEASES (semver, v-prefix)
--------------------------------------------------------------------------------
  v1.0.0 - 2026-08-12  (commit 645a418)
    First public release. Tagged + GitHub release published.
    Title used: "VDatum Batch Transform v1.0.0".

  v1.0.1 - 2026-08-12  (commit b481f1d)
    Maintenance/packaging patch, NO transform-logic changes:
      - Ship empty data/ and output/ dirs, each with a README.md (Git can't
        track empty dirs). .gitignore uses dir/* + !dir/README.md so the dir
        READMEs are tracked while working data/results stay ignored.
      - Added CHANGELOG.md (Keep a Changelog format).
      - Consolidated release notes into a single RELEASE_NOTES.md (newest
        first; replaced RELEASE_NOTES_v1.0.0.md via rename).
      - Doc/config metadata edits (README.md, config.ini).
    NOTE: v1.0.1 commit was made via `git commit --amend` + `push
    --force-with-lease` over the prior 1a620ae (solo repo, safe). Orphaned
    1a620ae will be GC'd by GitHub. LESSON going forward: once a commit is
    pushed/tagged, prefer follow-up commits over amend to keep tags anchored.
    Title to use: "VDatum Batch Transform v1.0.1"; body = the v1.0.1 section
    of RELEASE_NOTES.md.

  v1.0.2 - SUPERSEDED / abandoned. The tag was created on an incomplete commit
    (before the README.md edit was staged), then the same doc set went out under
    v1.0.3. The stray v1.0.2 tag was deleted (local + remote). LESSON: create
    the tag AFTER the final commit; if a tag already exists, bump the version
    rather than reusing/force-moving it.

  v1.0.3 - 2026-08-12  (commit 2861958)  <-- CURRENT / shipped to client
    Documentation patch, no functional change: made the ONE-FILE-PER-RUN input
    contract explicit in README.md (Input format), data/README.md, and
    config.ini [input]. Tool processes only the single file named in
    [input] file; it does NOT scan a directory. Multiple sheets -> run once per
    file, each with its own [output] basename. Supersedes incomplete v1.0.2.
    Title: "VDatum Batch Transform v1.0.3"; body = v1.0.3 section of
    RELEASE_NOTES.md.

  RELEASE FLOW (for future versions):
    1. Land changes; move CHANGELOG [Unreleased] -> [x.y.z] + date; add a
       matching section at the TOP of RELEASE_NOTES.md.
    2. commit + push.
    3. git tag -a vX.Y.Z -m "..." ; git push origin vX.Y.Z   (tag AFTER commit)
    4. GitHub: Draft release on tag vX.Y.Z, title "VDatum Batch Transform
       vX.Y.Z", paste that version's RELEASE_NOTES.md section.








--------------------------------------------------------------------------------
CURRENT RESULTS (PJH_Points, 107 stations, 2026-08-11)
--------------------------------------------------------------------------------
  OK (COOPS)            11   geodetic came straight from T&C
  VDATUM_FALLBACK       16   filled via VDatum
  VDATUM_OUT_OF_DOMAIN   2   honest -999999 for every region (Nacote/Wading R,
                             Mullica/Great Bay inland). NULL, continued.
  VDATUM_API_ERROR       8   every region returned a VDatum server/API fault
                             ("Uncaught error, please contact NOAA VDatum Program
                             Support team." / 412). Great Egg Harbor & upper
                             Delaware R points. NULL, continued. Note field lists
                             every region:frame tried + the exact server message.
  NO_TIDAL              70   station has no published datums in CO-OPS at all
                             (subordinate/prediction-only sites) -> NULL, halted.

  STATUS SPLIT (new): former single VDATUM_FAIL is now split into
  VDATUM_OUT_OF_DOMAIN (clean -999999) vs VDATUM_API_ERROR (server fault / 412).
  The "Uncaught error" is a VDatum-side 500-class fault, NOT an out-of-domain;
  worth reporting to VDatum support with the exact URL (now in the logfile).

  LOGFILE: output/<basename>.log — every API request URL (region+frame+params)
  and raw response; console shows per-station summary. Reproducible record.

  Sanity check: Sea Bright 8531804 — T&C MLLW=0.764, NAVD88(rel MLLW)=0.651;
  VDatum on a fallback neighbor matched to ~1 cm. Good.

  HORIZONTAL-FRAME DECISION (settled): the region:FRAME override (e.g.
  chesapeak_delaware:IGS14) declares coords as the region's required frame
  rather than doing a true NAD83(2011)->IGS14 pre-conversion. The ~1-2 m
  horizontal mismatch is far inside the datums team's loose ~50 m horizontal
  tolerance and well below the tidal grid uncertainty -> negligible. No proper
  pre-conversion step is warranted. Kept the override; documented the caveat.


--------------------------------------------------------------------------------
TO DO / NEXT
--------------------------------------------------------------------------------
  [ ] User to review VDATUM_API_ERROR (8) + VDATUM_OUT_OF_DOMAIN (2) +
      NO_TIDAL (70) exception lists (all expected). The 8 API_ERROR are
      VDatum-side "Uncaught error" faults at Great Egg Harbor / upper Delaware
      R points — candidates to report to VDatum support (exact URLs in log).
  [ ] git init here; create PRIVATE GitHub repo (namurry-noaa), SSH; stage
      (assistant may add, USER commits/pushes per 3431 rules).
  [ ] Deploy copy to Prod (~/NOAA/Coding/Python/) once accepted.
  [ ] Possible: pick a few clean points from this set as public-tool examples.
  [ ] OPEN IDEA (discussed, not yet built): a --recheck-failures mode that
      re-runs only prior VDATUM_API_ERROR rows against fresh output; pairs with
      "re-run when the cranky VDatum service comes back up."
  [ ] Possible enhancements: async/parallel with backoff; cache MDAPI results;
      add MHW/MHHW targets; optional per-station region column.

  RULES REMINDER (from 3431_system_summary.claude, TIGHTENED for this project):
   - USER runs ALL git (init/add/commit/push). ASSISTANT runs NO git commands
     at all — not even 'git add'. User is deliberately practicing git.
     Assistant's job: prep files + print the exact command sequence on request.
   - CONDA over pip; do NOT `conda update --all`.
   - This *_summary.claude stays LOCAL/gitignored.

--------------------------------------------------------------------------------
STATE AT END OF SESSION 2026-08-11 (night) — everything working, nothing committed
--------------------------------------------------------------------------------
  FUNCTIONAL & TESTED end-to-end:
    - CO-OPS MDAPI datums pull -> tidal(MLLW) + geodetic(NAVD88) rel. zero-plane
    - VDatum fallback with region=auto + region:FRAME overrides
    - dual sign columns (VDatum_raw up-neg; ST_NAVD88_VDatum T&C up-pos)
    - failure split: VDATUM_OUT_OF_DOMAIN vs VDATUM_API_ERROR (detailed Notes)
    - full DEBUG logfile (every request URL + response)
    - canary health check (startup / first-error / end) + "LIKELY DOWN" banner
      + exit code 3; verified via simulated outage AND live run (38% err, UP)
  FILES: vdatum_batch_transform.py, config.ini, environment.yml, README.md,
    LICENSE, .gitignore, data/ (source, read-only), output/ (gitignored).
  ENV: conda env 'vdatum_batch_xform' (Py 3.12; pandas, requests, odfpy) — built.
  NOT DONE YET: git init, GitHub repo, Prod deploy (all queued in TO DO above).

  >>> NEXT SESSION STARTUP CHECKLIST (print this first):
      1. Read this summary + confirm nothing changed on disk overnight.
      2. Activate env:  conda activate vdatum_batch_xform
      3. Confirm the tool still runs:
           python vdatum_batch_transform.py --config config.ini --limit 6
         (expect canary UP, a couple VDATUM_FALLBACK, exit 0)
      4. Review exception lists with user (API_ERROR / OUT_OF_DOMAIN / NO_TIDAL).
      5. Decide: build --recheck-failures mode? add MHW/MHHW targets?
      6. When user is ready: git init here, stage files (assistant adds; USER
         commits/pushes), create PRIVATE GitHub repo (namurry-noaa, SSH).
       7. After acceptance: deploy copy to ~/NOAA/Coding/Python/ (Prod).
================================================================================

================================================================================
v2.0.0 REDESIGN (2026-08-12) — CURRENT DESIGN (supersedes all v1 pipeline/cols)
================================================================================
WHY: v1 halted a station (NO_TIDAL) if CO-OPS lacked the tidal datum, which
  discarded ~70/107 PJH stations. VDatum can MODEL the geodetic value with the
  tidal datum as the 0 plane, so we now use it and keep far more stations.

PIPELINE (single tidal datum -> single geodetic datum = NAVD88):
  Tidal datum (the "zero plane"), INI-selectable: MLLW | MLW | MHW | MHHW | LMSL
    - Observed values come ONLY from CO-OPS.
        present -> ST_<TIDAL>=value, <TIDAL>_Source=COOPS
        absent  -> ST_<TIDAL>=0, <TIDAL>_Source=VDATUM_ZERO, Notes explains the
                   datum is the 0 plane for the VDatum transform (NOT observed).
    - LMSL nuance: CO-OPS publishes it as "MSL"; tool maps LMSL->MSL for the
      CO-OPS lookup (COOPS_ALIAS), but keeps "LMSL" as the VDatum source frame
      and in the column name. (Verified: Sea Bright LMSL=1.342 from CO-OPS MSL.)
  Geodetic (NAVD88) relative to the tidal zero plane:
    - CO-OPS has both -> ST_NAVD88 = NAVD88 - tidal (COOPS). NAVD88_Source=COOPS.
    - else VDatum: transform (tidal_datum, s_z=0) -> NAVD88 at lat/lon.
        VDatum is up-is-NEGATIVE; we SIGN-FLIP (ST_NAVD88 = -t_z) to CO-OPS
        up-is-POSITIVE. NAVD88_Source=VDATUM; VDatum_uncertainty = the OUTPUT
        uncertainty VDatum reports (all uncertainty is on the output side
        because the tidal datum IS the 0 plane -> input carries none).
        out of domain / persistent server fault -> station -> EXCEPTIONS.
  EXCEPTIONS only when: no CO-OPS NAVD88 AND VDatum can't transform the point.
  A station can be VDATUM_ZERO tidal yet still get a valid modeled NAVD88 (stays
  in RESULTS).

INTERNAL QC CROSS-CHECK ([qc] crosscheck_coops, default true):
  When BOTH tidal and NAVD88 come from CO-OPS, also call VDatum and compare.
  CO-OPS is ALWAYS retained; note-only. Flags if |COOPS - VDatum_modeled| >
  VDatum's reported uncertainty (likely VDatum grid anomaly). It's framed as a
  VDATUM check (we trust observed CO-OPS offsets). Check-call failures are
  advisory and do NOT feed the "VDatum likely down" verdict (not added to
  vd_outcomes). Low-key in README; not a user-facing feature.

OUTPUT — TWO CSVs, shared columns:
  passthrough..., Station ID, Latitude, Longitude,
  ST_<TIDAL> (u), <TIDAL>_Source, ST_NAVD88 (u), NAVD88_Source,
  VDatum_uncertainty (u), Notes
    <basename>.csv            = stations that got NAVD88 (status OK)
    <basename>_exceptions.csv = no NAVD88 (status NO_GEODETIC / COOPS_ERROR)
  Plus <basename>_vdatum_bug_report.csv (persistent server faults + URLs) and
  <basename>.log. Status vocabulary now: OK | NO_GEODETIC | COOPS_ERROR.

CODE CHANGES (vdatum_batch_transform.py, full rewrite of core):
  - Config: tidal_datum validated against VALID_TIDAL_DATUMS; single
    geodetic_datum (dropped geodetic_datums list + per-target blocks). New
    [qc] crosscheck_coops. Dropped [vdatum] s_v_frame (source frame = tidal).
  - vdatum_convert() -> vdatum_transform(session,lat,lon,s_v_frame,t_v_frame,cfg).
  - StationRow flattened (no TargetResult); _crosscheck() helper; _note_append().
  - write_outputs() splits OK vs exceptions; _header()/_record() new schema.
  - --recheck-failures reads BOTH results+exceptions CSVs, reruns NO_GEODETIC.
  - Kept: smart region fallthrough, region:FRAME, canary/down-detection,
    server retry+bug report, logging, csv/ods/xlsx input, one-file-per-run.

TESTED (2026-08-12, VDatum intermittently cranky):
  - limit 8: all 8 OK; mix of MLLW COOPS / VDATUM_ZERO; NAVD88 COOPS+VDATUM.
    Sea Bright QC note "agrees within uncertainty (diff 0.012 m, unc 0.073 m)".
    Atlantic Highlands VDATUM_ZERO with modeled NAVD88=0.851 (unc 0.062) + note.
  - full 107: Results 59 (11 COOPS + 48 VDatum), Exceptions 48 (mostly the
    transient "Uncaught error" faults this session; canary UP -> point-specific,
    not an outage). vs v1 which had only 27 usable. Big fill improvement.
  - --recheck-failures ran (48 retried, still 100% err THIS session = VDatum
    cranky), 107 rows preserved. Merge logic OK.
  - LMSL mapping verified.
  NOTE for client note: the 48 exceptions are largely recoverable via
  --recheck-failures when VDatum is healthy; not true out-of-domain.

DOCS UPDATED: README (pipeline, sign convention, QC, two-CSV schema, LMSL,
  config), config.ini ([datums] tidal choices, [qc], [vdatum] simplified),
  CHANGELOG [2.0.0], RELEASE_NOTES v2.0.0, examples/README, output/README.

RELEASE PLAN: this is v2.0.0 (BREAKING: config keys + CSV columns changed).
  Not yet committed/tagged. Follow the RELEASE FLOW above; tag AFTER commit.
  Prod (~/NOAA/Coding/Python/VDatum_Batch_Transform) still on v1 output schema
  and has its own config (basename user_transformed_pts) — re-sync after commit.
================================================================================

================================================================================
SESSION CLOSE (8/12/2026) — v2.x SHIPPED & DELIVERED TO CLIENT
================================================================================
STATUS: DONE for now. Working tree clean; all released and pushed.

RELEASES (all tagged, pushed, GitHub releases published; tags anchored to HEAD):
  v2.0.0  d63172e  Major redesign (VDatum models NAVD88; single tidal->geodetic;
                   two-CSV output; VDATUM_ZERO; QC cross-check). BREAKING.
  v2.0.1  91913d3  Dates switched to M/D/YYYY in CHANGELOG + RELEASE_NOTES.
  v2.0.2           README copyedit (typos/wording). Docs only.
  v2.0.3           Dates REVERTED to ISO YYYY-MM-DD (undoes 2.0.1); summary file
                   moved from *_summary.claude (local) to *_summary.md (tracked).
  DATE FORMAT: ISO YYYY-MM-DD everywhere (Keep-a-Changelog standard). The brief
    M/D/YYYY experiment in 2.0.1 was reverted in 2.0.3 — keep ISO going forward.

PROD: ~/NOAA/Coding/Python/VDatum_Batch_Transform synced to v2 (Option B full
  mirror, .git/__pycache__ excluded). Prod config.ini is the LIVE working copy
  (its own basename/input/tidal_datum) — on syncs, EXCLUDE config.ini unless a
  new INI key was added (v2 added [qc] + [datums] changes, which is why the last
  sync included it). User manually copies README to Prod for doc-only patches.

CLIENT: full run reviewed, email sent with results. Plain-language items covered
  in the note: pipeline (tidal-from-CO-OPS, VDatum-modeled otherwise),
  IGS14/region caveat (~1-2 m horizontal, negligible vs VDatum cm-level output
  uncertainty and the ~50 m tidal-referencing tolerance), results/exceptions
  split, and "re-run --recheck-failures when VDatum recovers".

OPEN / FUTURE (not started; may revisit):
  - Send the VDatum bug-report CSV (persistent "Uncaught error" points) to the
    NOAA VDatum Support team.
  - Possible future features (would be v2.1+ or v3): NAPGD2022 target (needs the
    other repo's extractor; not on VDatum API yet), MDAPI caching, more geodetic
    targets. Guardrail still stands: make batch transforms easier, do NOT build
    a "VDatum lite".

SUMMARY FILE: now tracked as VDatum_Batch_Transform_summary.md and committed
  (was a gitignored *_summary.claude). Being formalized toward a structured
  project overview / near-"agent skill". .gitignore still ignores the legacy
  *_summary.claude pattern only.

GIT RULE (reaffirmed): USER runs ALL git; assistant edits files + prints exact
  command sequences on request. Tag AFTER commit (lesson from the 1.0.1/1.0.2
  tag mishaps).
================================================================================
