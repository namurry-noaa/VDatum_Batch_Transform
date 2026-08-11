#!/usr/bin/env python3
"""
VDatum Batch Transform
======================

Batch-transform a single tidal datum to a geodetic datum (NAVD88) for a list of
NOAA CO-OPS tide stations, using the CO-OPS Metadata API with a NOAA VDatum API
fallback.

Pipeline (per station)
-----------------------
Tidal datum (the "zero plane"), INI-selectable (MLLW | MLW | MHW | MHHW | LMSL):
  * Pulled from CO-OPS. Observed values ONLY ever come from CO-OPS.
      - present -> ST_<TIDAL> = observed value, <TIDAL>_Source = COOPS
      - absent  -> ST_<TIDAL> = 0, <TIDAL>_Source = VDATUM_ZERO, with a Notes
                   entry explaining the tidal datum is used as the 0 reference
                   plane for the VDatum transform (NOT an observed value).

Geodetic datum (NAVD88), expressed relative to the tidal zero-plane:
  * CO-OPS publishes it -> ST_NAVD88 = NAVD88 - tidal(STND), NAVD88_Source = COOPS
  * else VDatum: transform (tidal_datum, s_z=0) -> NAVD88 at the station's
    lat/lon. VDatum reports the geodetic height of the tidal=0 surface in its
    own up-is-NEGATIVE convention; we SIGN-FLIP to the CO-OPS up-is-POSITIVE
    convention for ST_NAVD88. NAVD88_Source = VDATUM, and VDatum_uncertainty is
    the uncertainty VDatum reports for that OUTPUT transform (all uncertainty
    lives on the output side because the tidal datum is defined as the 0 plane).
      - out of VDatum domain / persistent server fault -> station -> exceptions.

Internal QC cross-check (optional, [qc] crosscheck_coops, default on)
  When BOTH the tidal datum AND NAVD88 come from CO-OPS, additionally query
  VDatum for the modeled value and compare. CO-OPS observations are trusted and
  ALWAYS retained; the check is advisory and only adds a Notes entry, flagging
  when the modeled value differs from CO-OPS by more than VDatum's reported
  uncertainty (a likely VDatum grid anomaly at that point). Check-call failures
  are advisory and do not affect the "VDatum likely down" verdict.

On the VDatum sign convention ("up is negative"), see the last FAQ at:
    https://vdatum.noaa.gov/docs/faqs.html

Outputs (CSV, meters or feet per config) into the configured output dir:
  <basename>.csv            stations that obtained NAVD88 (from CO-OPS or VDatum)
  <basename>_exceptions.csv stations with no CO-OPS NAVD88 AND out of VDatum range
  <basename>_vdatum_bug_report.csv  persistent VDatum server faults + failing URLs
  <basename>.log            full per-run request/response log

The source table is treated as READ-ONLY and is never modified.
Reusable: everything is driven by an INI config file (see config.ini).
Author: NOAA/NOS/CO-OPS. Public domain (U.S. Government work).
"""

from __future__ import annotations

import argparse
import configparser
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import requests

MDAPI_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{sid}/datums.json"
VDATUM_URL = "https://vdatum.noaa.gov/vdatumweb/api/convert"
VDATUM_NODATA = -999999.0

# Tidal datums accepted as the input "zero plane".
VALID_TIDAL_DATUMS = ("MLLW", "MLW", "MHW", "MHHW", "LMSL")
# CO-OPS abbreviation for local mean sea level differs from VDatum's frame name.
# CO-OPS publishes "MSL"; VDatum's vertical frame is "LMSL".
COOPS_ALIAS = {"LMSL": "MSL"}

# Module logger. Handlers are attached in setup_logging(); until then, calls
# are silently dropped (NullHandler) so importing the module is side-effect-free.
log = logging.getLogger("vdatum_batch_transform")
log.addHandler(logging.NullHandler())


def setup_logging(logfile_path: str, verbose: bool = True) -> None:
    """Attach a file handler (full detail) and a console handler (summary)."""
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        if not isinstance(h, logging.NullHandler):
            log.removeHandler(h)

    fh = logging.FileHandler(logfile_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # input
    in_file: str
    sheet: str
    station_id_col: str
    lat_col: str
    lon_col: str
    passthrough_cols: list[str]
    # output
    out_dir: str
    basename: str
    units: str  # "metric" | "english"
    # datums
    tidal_datum: str        # single tidal zero-plane (MLLW|MLW|MHW|MHHW|LMSL)
    geodetic_datum: str     # single geodetic target (e.g. NAVD88)
    # vdatum
    s_h_frame: str
    epoch_in: str
    epoch_out: str
    geoid: str
    region: str
    region_try_order: list[str]
    # api
    application: str
    sleep_between_calls: float
    timeout: float
    max_retries: int
    vdatum_server_retries: int
    # qc
    crosscheck_coops: bool
    # health / canary
    canary_enabled: bool
    canary_region: str
    canary_s_x: float
    canary_s_y: float
    canary_s_v_frame: str
    canary_t_v_frame: str
    down_error_rate: float

    @property
    def vdatum_units(self) -> str:
        return "m" if self.units == "metric" else "us_ft"

    @property
    def coops_tidal_name(self) -> str:
        """CO-OPS datum abbreviation for the configured tidal datum."""
        return COOPS_ALIAS.get(self.tidal_datum, self.tidal_datum)


def _split(csv_str: str) -> list[str]:
    return [s.strip() for s in csv_str.split(",") if s.strip()]


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not cp.read(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    tidal = cp.get("datums", "tidal_datum").strip().upper()
    if tidal not in VALID_TIDAL_DATUMS:
        raise ValueError(
            f"tidal_datum {tidal!r} is not supported. "
            f"Choose one of: {', '.join(VALID_TIDAL_DATUMS)}.")

    return Config(
        in_file=cp.get("input", "file"),
        sheet=cp.get("input", "sheet"),
        station_id_col=cp.get("input", "station_id_col"),
        lat_col=cp.get("input", "latitude_col"),
        lon_col=cp.get("input", "longitude_col"),
        passthrough_cols=_split(cp.get("input", "passthrough_cols", fallback="")),
        out_dir=cp.get("output", "dir"),
        basename=cp.get("output", "basename"),
        units=cp.get("output", "units", fallback="metric").strip().lower(),
        tidal_datum=tidal,
        geodetic_datum=cp.get("datums", "geodetic_datum", fallback="NAVD88").strip().upper(),
        s_h_frame=cp.get("vdatum", "s_h_frame"),
        epoch_in=cp.get("vdatum", "epoch_in"),
        epoch_out=cp.get("vdatum", "epoch_out"),
        geoid=cp.get("vdatum", "geoid"),
        region=cp.get("vdatum", "region", fallback="auto").strip().lower(),
        region_try_order=_split(cp.get("vdatum", "region_try_order", fallback="contiguous")),
        application=cp.get("api", "application", fallback="CO-OPS_VDatum_Batch_Transform"),
        sleep_between_calls=cp.getfloat("api", "sleep_between_calls", fallback=0.5),
        timeout=cp.getfloat("api", "timeout", fallback=30.0),
        max_retries=cp.getint("api", "max_retries", fallback=3),
        vdatum_server_retries=cp.getint("api", "vdatum_server_retries", fallback=3),
        crosscheck_coops=cp.getboolean("qc", "crosscheck_coops", fallback=True),
        canary_enabled=cp.getboolean("health", "canary_enabled", fallback=True),
        canary_region=cp.get("health", "canary_region", fallback="contiguous"),
        canary_s_x=cp.getfloat("health", "canary_s_x", fallback=-75.211),
        canary_s_y=cp.getfloat("health", "canary_s_y", fallback=36.129),
        canary_s_v_frame=cp.get("health", "canary_s_v_frame", fallback="NAVD88"),
        canary_t_v_frame=cp.get("health", "canary_t_v_frame", fallback="MLLW"),
        down_error_rate=cp.getfloat("health", "down_error_rate", fallback=0.90),
    )


# --------------------------------------------------------------------------- #
# API clients
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    pass


def _get_json(session: requests.Session, url: str, params: dict, cfg: Config) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            r = session.get(url, params=params, timeout=cfg.timeout)
            log.debug("GET %s  -> HTTP %s", r.url, r.status_code)
            r.raise_for_status()
            data = r.json()
            log.debug("  response: %s", data)
            return data
        except (requests.RequestException, ValueError) as e:
            last_err = e
            log.debug("  attempt %d/%d failed: %s", attempt, cfg.max_retries, e)
            if attempt < cfg.max_retries:
                time.sleep(cfg.sleep_between_calls * attempt)
    raise ApiError(f"request failed after {cfg.max_retries} tries: {last_err}")


def fetch_coops_datums(session: requests.Session, sid: str, cfg: Config) -> dict:
    """Return {DATUM_ABBR: value} from CO-OPS MDAPI, in the configured units.

    Empty dict if the station has no published datums.
    """
    params = {"units": cfg.units}
    data = _get_json(session, MDAPI_URL.format(sid=sid), params, cfg)
    out: dict[str, float] = {}
    for d in (data.get("datums") or []):
        name = str(d.get("name", "")).upper()
        val = d.get("value")
        if name and val is not None:
            try:
                out[name] = float(val)
            except (TypeError, ValueError):
                pass
    return out


@dataclass
class VdatumResult:
    t_z: Optional[float] = None      # raw VDatum value (up-is-negative convention)
    uncertainty: Optional[float] = None
    region: Optional[str] = None
    ok: bool = False
    # failure_kind (when not ok):
    #   "OUT_OF_DOMAIN" -> every applicable region was -999999 / geographic miss
    #   "API_ERROR"     -> persistent SERVER fault or HTTP/transport error
    failure_kind: str = ""
    message: str = ""
    server_faults: list = field(default_factory=list)


def _classify_vdatum_message(msg: str) -> str:
    """Bucket a VDatum error 'message': GEOGRAPHIC | SERVER | OTHER."""
    m = (msg or "").lower()
    if "uncaught error" in m:
        return "SERVER"
    geographic_markers = (
        "input region is not correct",
        "should be igs14 for tidal",
        "only cover",
        "unsupported vertical datum",
        "out of the region",
        "not in the region",
    )
    if any(k in m for k in geographic_markers):
        return "GEOGRAPHIC"
    return "OTHER"


def vdatum_transform(session: requests.Session, lat: float, lon: float,
                     s_v_frame: str, t_v_frame: str, cfg: Config) -> VdatumResult:
    """Transform (s_v_frame, s_z=0) -> t_v_frame at (lat, lon).

    Smart region fallthrough: a clean -999999 or definitive geographic rejection
    means the region simply doesn't apply here (move on quietly); the generic
    "Uncaught error" SERVER fault is retried with backoff and, if persistent,
    captured for the bug report.
    """
    regions = cfg.region_try_order if cfg.region == "auto" else [cfg.region]
    saw_server_error = False
    attempts: list[str] = []
    server_faults: list[dict] = []

    for region_spec in regions:
        if ":" in region_spec:
            region, s_h_frame = (p.strip() for p in region_spec.split(":", 1))
        else:
            region, s_h_frame = region_spec, cfg.s_h_frame
        params = {
            "region": region,
            "s_x": f"{lon}",
            "s_y": f"{lat}",
            "s_z": "0.0",
            "s_h_frame": s_h_frame,
            "s_coor": "geo",
            "s_v_frame": s_v_frame,
            "s_v_unit": cfg.vdatum_units,
            "s_v_geoid": cfg.geoid,
            "t_h_frame": s_h_frame,
            "t_coor": "geo",
            "t_v_frame": t_v_frame,
            "t_v_unit": cfg.vdatum_units,
            "t_v_geoid": cfg.geoid,
            "epoch_in": cfg.epoch_in,
            "epoch_out": cfg.epoch_out,
        }
        tag = f"{region}/{s_h_frame}"

        data = None
        transport_err = None
        for server_try in range(1, cfg.vdatum_server_retries + 1):
            try:
                data = _get_json(session, VDATUM_URL, params, cfg)
            except ApiError as e:
                transport_err = str(e)
                data = None
                break
            msg = data.get("message")
            has_error = data.get("errorCode") or (msg and data.get("t_z") is None)
            if has_error and _classify_vdatum_message(msg) == "SERVER":
                if server_try < cfg.vdatum_server_retries:
                    backoff = cfg.sleep_between_calls * (2 ** (server_try - 1))
                    log.debug("  VDatum %s -> SERVER fault (try %d/%d), backoff %.1fs",
                              tag, server_try, cfg.vdatum_server_retries, backoff)
                    time.sleep(backoff)
                    continue
            break

        if data is None:
            saw_server_error = True
            attempts.append(f"{tag}: HTTP/transport error: {transport_err}")
            server_faults.append({"region": region, "s_h_frame": s_h_frame,
                                  "kind": "transport", "message": transport_err,
                                  "url": _vdatum_url(params)})
            log.debug("  VDatum %s -> transport error: %s", tag, transport_err)
            time.sleep(cfg.sleep_between_calls)
            continue

        msg = data.get("message")
        has_error = data.get("errorCode") or (msg and data.get("t_z") is None)
        if has_error:
            code = data.get("errorCode", "")
            category = _classify_vdatum_message(msg)
            if category == "SERVER":
                saw_server_error = True
                attempts.append(f"{tag}: SERVER fault ({msg})")
                server_faults.append({"region": region, "s_h_frame": s_h_frame,
                                      "kind": "server", "message": msg,
                                      "url": _vdatum_url(params)})
                log.debug("  VDatum %s -> persistent SERVER fault: %s", tag, msg)
            else:
                attempts.append(f"{tag}: region N/A ({msg})")
                log.debug("  VDatum %s -> region not applicable: %s", tag, msg)
            time.sleep(cfg.sleep_between_calls)
            continue

        t_z_raw = data.get("t_z")
        try:
            t_z = float(t_z_raw)
        except (TypeError, ValueError):
            saw_server_error = True
            attempts.append(f"{tag}: non-numeric t_z: {t_z_raw!r}")
            server_faults.append({"region": region, "s_h_frame": s_h_frame,
                                  "kind": "server", "message": f"non-numeric t_z: {t_z_raw!r}",
                                  "url": _vdatum_url(params)})
            log.debug("  VDatum %s -> non-numeric t_z: %r", tag, t_z_raw)
            time.sleep(cfg.sleep_between_calls)
            continue

        if t_z <= VDATUM_NODATA + 1:  # -999999 no-data
            attempts.append(f"{tag}: -999999 (out of domain)")
            log.debug("  VDatum %s -> -999999 (out of domain)", tag)
            time.sleep(cfg.sleep_between_calls)
            continue

        unc = data.get("uncertainty")
        try:
            unc = float(unc) if unc not in (None, "") else None
        except (TypeError, ValueError):
            unc = None

        log.debug("  VDatum %s -> t_z=%s unc=%s (success)", tag, t_z, unc)
        return VdatumResult(t_z=t_z, uncertainty=unc, region=region, ok=True)

    kind = "API_ERROR" if saw_server_error else "OUT_OF_DOMAIN"
    detail = "; ".join(attempts) if attempts else "no region succeeded"
    return VdatumResult(ok=False, failure_kind=kind, message=detail,
                        server_faults=server_faults)


def _vdatum_url(params: dict) -> str:
    """Reconstruct the full VDatum GET URL for logging / bug reports."""
    from urllib.parse import urlencode
    return f"{VDATUM_URL}?{urlencode(params)}"


def vdatum_canary(session: requests.Session, cfg: Config) -> tuple[bool, str]:
    """Probe VDatum with a known-good point. Returns (is_up, message)."""
    params = {
        "region": cfg.canary_region,
        "s_x": f"{cfg.canary_s_x}",
        "s_y": f"{cfg.canary_s_y}",
        "s_z": "0.0",
        "s_h_frame": cfg.s_h_frame,
        "s_coor": "geo",
        "s_v_frame": cfg.canary_s_v_frame,
        "s_v_unit": cfg.vdatum_units,
        "s_v_geoid": cfg.geoid,
        "t_h_frame": cfg.s_h_frame,
        "t_coor": "geo",
        "t_v_frame": cfg.canary_t_v_frame,
        "t_v_unit": cfg.vdatum_units,
        "t_v_geoid": cfg.geoid,
    }
    try:
        data = _get_json(session, VDATUM_URL, params, cfg)
    except ApiError as e:
        return False, f"canary transport/HTTP error: {e}"
    if data.get("errorCode") or (data.get("message") and data.get("t_z") is None):
        return False, f"canary API error: {data.get('message', 'unknown')}"
    try:
        t_z = float(data.get("t_z"))
    except (TypeError, ValueError):
        return False, f"canary non-numeric t_z: {data.get('t_z')!r}"
    if t_z <= VDATUM_NODATA + 1:
        return False, ("canary returned -999999 for a known-good point "
                       "(unexpected; possible grid/service issue)")
    return True, f"canary OK (t_z={t_z})"


# --------------------------------------------------------------------------- #
# Core per-station processing
# --------------------------------------------------------------------------- #
@dataclass
class StationRow:
    passthrough: dict = field(default_factory=dict)
    station_id: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # tidal (zero-plane)
    st_tidal: Optional[float] = None            # value in ST_<TIDAL> column
    tidal_source: str = ""                      # COOPS | VDATUM_ZERO
    # geodetic (NAVD88 relative to tidal zero-plane)
    st_geodetic: Optional[float] = None
    geodetic_source: str = ""                   # COOPS | VDATUM
    vdatum_uncertainty: Optional[float] = None
    vdatum_region: Optional[str] = None
    # bookkeeping
    status: str = ""    # OK | NO_GEODETIC | COOPS_ERROR
    note: str = ""
    server_faults: list = field(default_factory=list)
    # outcome tags for run-level VDatum health tracking (fallback calls only)
    vd_outcomes: list = field(default_factory=list)

    @property
    def is_exception(self) -> bool:
        """Needs review: no NAVD88 obtainable, or a CO-OPS fetch error."""
        return self.status != "OK"


def _note_append(sr: StationRow, text: str) -> None:
    sr.note = f"{sr.note} {text}".strip() if sr.note else text


def process_station(session: requests.Session, sr: StationRow, cfg: Config,
                    recheck: bool = False) -> None:
    """Populate sr in place (single tidal -> NAVD88 pipeline).

    recheck: if True, only re-attempt the VDatum geodetic fallback for a station
    previously marked NO_GEODETIC; otherwise process from scratch.
    """
    tidal_name = cfg.coops_tidal_name          # CO-OPS abbreviation (MSL for LMSL)
    tidal_frame = cfg.tidal_datum              # VDatum source frame (LMSL etc.)
    geo = cfg.geodetic_datum

    try:
        datums = fetch_coops_datums(session, sr.station_id, cfg)
    except ApiError as e:
        sr.status = "COOPS_ERROR"
        sr.note = f"CO-OPS datums fetch failed: {e}"
        log.warning("Station %s: COOPS_ERROR: %s", sr.station_id, e)
        return

    # reset (supports recheck reruns)
    sr.st_tidal = None
    sr.tidal_source = ""
    sr.st_geodetic = None
    sr.geodetic_source = ""
    sr.vdatum_uncertainty = None
    sr.vdatum_region = None
    sr.note = ""
    sr.server_faults = []
    sr.vd_outcomes = []

    # --- Tidal datum (zero plane). Observed only from CO-OPS. ---
    tidal = datums.get(tidal_name)
    if tidal is not None:
        sr.st_tidal = round(tidal, 4)
        sr.tidal_source = "COOPS"
    else:
        sr.st_tidal = 0.0
        sr.tidal_source = "VDATUM_ZERO"
        _note_append(sr, f"No CO-OPS {cfg.tidal_datum} datum; {cfg.tidal_datum} "
                         f"used as the 0 reference plane for the VDatum transform "
                         f"(not an observed value).")

    # --- Geodetic (NAVD88 relative to tidal zero-plane) ---
    geodetic_stnd = datums.get(geo)
    if geodetic_stnd is not None and tidal is not None:
        # Both observed in CO-OPS: authoritative value.
        sr.st_geodetic = round(geodetic_stnd - tidal, 4)
        sr.geodetic_source = "COOPS"
        sr.status = "OK"
        # Optional internal QC cross-check against VDatum's modeled value.
        if cfg.crosscheck_coops and sr.latitude is not None and sr.longitude is not None:
            _crosscheck(session, sr, tidal_frame, geo, cfg)
        return

    # Need VDatum for the geodetic value.
    if sr.latitude is None or sr.longitude is None:
        sr.status = "NO_GEODETIC"
        _note_append(sr, f"CO-OPS lacks {geo}; VDatum fallback needs lat/lon (missing).")
        log.warning("Station %s: NO_GEODETIC (missing lat/lon)", sr.station_id)
        return

    time.sleep(cfg.sleep_between_calls)
    vr = vdatum_transform(session, sr.latitude, sr.longitude, tidal_frame, geo, cfg)
    sr.server_faults = vr.server_faults
    if not vr.ok:
        sr.status = "NO_GEODETIC"
        sr.geodetic_source = "VDATUM"
        sr.vd_outcomes.append("API_ERROR" if vr.failure_kind == "API_ERROR" else "OUT_OF_DOMAIN")
        if vr.failure_kind == "OUT_OF_DOMAIN":
            _note_append(sr, f"CO-OPS lacks {geo}; point outside VDatum domain. {vr.message}")
        else:
            _note_append(sr, f"CO-OPS lacks {geo}; VDatum API/server error. {vr.message}")
        short = vr.message.split(";")[0].strip()
        log.warning("Station %s: NO_GEODETIC (%s): %s", sr.station_id, vr.failure_kind, short)
        return

    # Success. VDatum t_z is up-is-negative; flip to CO-OPS up-is-positive.
    sr.st_geodetic = round(-vr.t_z, 4)
    sr.geodetic_source = "VDATUM"
    sr.vdatum_uncertainty = vr.uncertainty
    sr.vdatum_region = vr.region
    sr.status = "OK"
    sr.vd_outcomes.append("OK")
    _note_append(sr, f"{geo} modeled by VDatum from {cfg.tidal_datum}=0 plane "
                     f"(value sign-flipped to CO-OPS up-positive convention).")


def _crosscheck(session: requests.Session, sr: StationRow,
                tidal_frame: str, geo: str, cfg: Config) -> None:
    """Advisory QC: compare CO-OPS NAVD88 against VDatum's modeled value.

    CO-OPS is always retained; this only adds a Notes entry (and flags when the
    modeled value differs by more than VDatum's reported uncertainty). Check-call
    failures are advisory and do NOT count toward the "VDatum likely down"
    verdict (they are not added to vd_outcomes).
    """
    time.sleep(cfg.sleep_between_calls)
    vr = vdatum_transform(session, sr.latitude, sr.longitude, tidal_frame, geo, cfg)
    # Preserve any server faults for the bug report, but do not flag the station.
    if vr.server_faults:
        sr.server_faults.extend(vr.server_faults)
    if not vr.ok:
        _note_append(sr, f"QC: VDatum cross-check unavailable ({vr.failure_kind}).")
        return
    modeled = -vr.t_z  # CO-OPS convention
    diff = abs(sr.st_geodetic - modeled)
    unc = vr.uncertainty
    if unc is not None and diff > unc:
        _note_append(sr, f"QC FLAG: VDatum modeled {geo} ({modeled:.3f}) differs from "
                         f"CO-OPS ({sr.st_geodetic:.3f}) by {diff:.3f} m (> VDatum "
                         f"uncertainty {unc:.3f} m) — possible VDatum grid anomaly; "
                         f"CO-OPS observed value retained.")
    else:
        u = f"{unc:.3f}" if unc is not None else "n/a"
        _note_append(sr, f"QC: VDatum modeled {geo} agrees within uncertainty "
                         f"(diff {diff:.3f} m, unc {u} m).")


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def read_input(cfg: Config) -> pd.DataFrame:
    """Load the source table. Accepts .csv, .ods, or .xlsx."""
    ext = os.path.splitext(cfg.in_file)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(cfg.in_file)
    elif ext in (".ods", ".xlsx"):
        engine = "odf" if ext == ".ods" else None
        df = pd.read_excel(cfg.in_file, sheet_name=cfg.sheet, engine=engine)
    else:
        raise ValueError(
            f"Unsupported input format {ext!r} for {cfg.in_file!r}. "
            f"Supported: .csv, .ods, .xlsx")

    required = {
        "station_id_col": cfg.station_id_col,
        "latitude_col": cfg.lat_col,
        "longitude_col": cfg.lon_col,
    }
    missing = {k: v for k, v in required.items() if v not in df.columns}
    if missing:
        where = f"{cfg.in_file}" + (f" [{cfg.sheet}]" if ext != ".csv" else "")
        lines = [f"Input file is missing required column(s) in {where}:"]
        for key, name in missing.items():
            lines.append(f"  - {name!r}  (config.ini [input] {key})")
        lines.append(f"Columns found: {list(df.columns)}")
        lines.append("Fix the header row in the file, or update the *_col names "
                     "in config.ini to match.")
        raise ValueError("\n".join(lines))

    for c in cfg.passthrough_cols:
        if c not in df.columns:
            log.warning("Pass-through column %r not found in input; it will be blank.", c)
    return df


def build_rows(df: pd.DataFrame, cfg: Config) -> list[StationRow]:
    rows: list[StationRow] = []
    for _, r in df.iterrows():
        sid_raw = r[cfg.station_id_col]
        if pd.isna(sid_raw):
            continue
        if isinstance(sid_raw, float) and sid_raw.is_integer():
            sid = str(int(sid_raw))
        else:
            sid = str(sid_raw).strip()

        def num(v):
            return float(v) if pd.notna(v) else None

        rows.append(StationRow(
            passthrough={c: (r[c] if c in df.columns and pd.notna(r[c]) else "")
                         for c in cfg.passthrough_cols},
            station_id=sid,
            latitude=num(r[cfg.lat_col]),
            longitude=num(r[cfg.lon_col]),
        ))
    return rows


def _header(cfg: Config) -> list[str]:
    u = "m" if cfg.units == "metric" else "ft"
    t = cfg.tidal_datum
    g = cfg.geodetic_datum
    return (
        cfg.passthrough_cols
        + ["Station ID", "Latitude", "Longitude",
           f"ST_{t} ({u})", f"{t}_Source",
           f"ST_{g} ({u})", f"{g}_Source",
           f"VDatum_uncertainty ({u})", "Notes"]
    )


def _record(sr: StationRow, cfg: Config) -> list:
    return (
        [sr.passthrough.get(c, "") for c in cfg.passthrough_cols]
        + [sr.station_id, sr.latitude, sr.longitude,
           sr.st_tidal, sr.tidal_source,
           sr.st_geodetic, sr.geodetic_source,
           sr.vdatum_uncertainty, sr.note]
    )


def write_outputs(rows: list[StationRow], cfg: Config) -> tuple[str, str]:
    os.makedirs(cfg.out_dir, exist_ok=True)
    header = _header(cfg)

    results_path = os.path.join(cfg.out_dir, f"{cfg.basename}.csv")
    exc_path = os.path.join(cfg.out_dir, f"{cfg.basename}_exceptions.csv")

    # Results = stations that obtained NAVD88 (status OK). Exceptions = the rest.
    results = [sr for sr in rows if sr.status == "OK"]
    exceptions = [sr for sr in rows if sr.is_exception]

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sr in results:
            w.writerow(_record(sr, cfg))

    with open(exc_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sr in exceptions:
            w.writerow(_record(sr, cfg))

    return results_path, exc_path


def write_vdatum_bug_report(rows: list[StationRow], cfg: Config) -> Optional[str]:
    """Write persistent VDatum SERVER/transport faults (one row per attempt),
    with the exact failing URL — to forward to the NOAA VDatum Support team."""
    fault_rows: list[dict] = []
    for sr in rows:
        for fault in sr.server_faults:
            fault_rows.append({
                "Station ID": sr.station_id,
                "Station Name": sr.passthrough.get("Station Name", ""),
                "Latitude": sr.latitude,
                "Longitude": sr.longitude,
                "Region": fault.get("region", ""),
                "s_h_frame": fault.get("s_h_frame", ""),
                "Fault kind": fault.get("kind", ""),
                "VDatum message": fault.get("message", ""),
                "Failing URL": fault.get("url", ""),
            })
    if not fault_rows:
        return None

    path = os.path.join(cfg.out_dir, f"{cfg.basename}_vdatum_bug_report.csv")
    cols = ["Station ID", "Station Name", "Latitude", "Longitude",
            "Region", "s_h_frame", "Fault kind", "VDatum message", "Failing URL"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(fault_rows)
    return path


def _load_prior_results(cfg: Config) -> list[dict]:
    """Read a prior results CSV (rows in file order) for recheck merges."""
    path = os.path.join(cfg.out_dir, f"{cfg.basename}.csv")
    exc = os.path.join(cfg.out_dir, f"{cfg.basename}_exceptions.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"--recheck-failures needs a prior results file: {path} (run a normal pass first).")
    out: list[dict] = []
    for p in (path, exc):
        if os.path.exists(p):
            with open(p, newline="", encoding="utf-8") as f:
                out.extend(csv.DictReader(f))
    return out


def _rows_from_prior(prior_rows: list[dict], cfg: Config) -> list[StationRow]:
    """Reconstruct StationRow objects from prior results + exceptions CSVs."""
    u = "m" if cfg.units == "metric" else "ft"
    t, g = cfg.tidal_datum, cfg.geodetic_datum

    def fnum(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    rows: list[StationRow] = []
    seen: set[str] = set()
    for r in prior_rows:
        sid = (r.get("Station ID") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        note = r.get("Notes", "")
        # Infer status: if a geodetic value exists, treat as OK; else NO_GEODETIC.
        st_geo = fnum(r.get(f"ST_{g} ({u})"))
        status = "OK" if st_geo is not None else "NO_GEODETIC"
        rows.append(StationRow(
            passthrough={c: r.get(c, "") for c in cfg.passthrough_cols},
            station_id=sid,
            latitude=fnum(r.get("Latitude")),
            longitude=fnum(r.get("Longitude")),
            st_tidal=fnum(r.get(f"ST_{t} ({u})")),
            tidal_source=r.get(f"{t}_Source", ""),
            st_geodetic=st_geo,
            geodetic_source=r.get(f"{g}_Source", ""),
            vdatum_uncertainty=fnum(r.get(f"VDatum_uncertainty ({u})")),
            status=status,
            note=note,
        ))
    return rows


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(cfg: Config, limit: Optional[int] = None, recheck_failures: bool = False) -> bool:
    """Execute the batch. Returns True if VDatum was judged LIKELY DOWN."""
    os.makedirs(cfg.out_dir, exist_ok=True)
    logfile_path = os.path.join(cfg.out_dir, f"{cfg.basename}.log")
    setup_logging(logfile_path)

    if recheck_failures:
        prior = _load_prior_results(cfg)
        rows = _rows_from_prior(prior, cfg)
        work_rows = [sr for sr in rows if sr.status == "NO_GEODETIC"]
        mode_desc = f"RECHECK: {len(work_rows)} station(s) previously NO_GEODETIC"
    else:
        df = read_input(cfg)
        rows = build_rows(df, cfg)
        if limit:
            rows = rows[:limit]
        work_rows = rows
        mode_desc = f"FULL: {len(rows)} station(s)"

    total = len(work_rows)
    log.info("VDatum Batch Transform  (log: %s)", logfile_path)
    log.info("Mode: %s", mode_desc)
    log.info("Input: %s [%s]", cfg.in_file, cfg.sheet)
    log.info("Tidal (zero-plane) datum: %s | Geodetic target: %s",
             cfg.tidal_datum, cfg.geodetic_datum)
    log.info("Units: %s | region: %s | QC cross-check: %s",
             cfg.units, cfg.region, "on" if cfg.crosscheck_coops else "off")
    log.info("Region try order: %s", ", ".join(cfg.region_try_order))
    log.info("")

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.application})

    canary_start = None
    if cfg.canary_enabled:
        up, msg = vdatum_canary(session, cfg)
        canary_start = up
        if up:
            log.info("VDatum canary (startup): UP — %s", msg)
        else:
            log.warning("VDatum canary (startup): FAILED — %s", msg)
            log.warning("*** VDatum may be down or cranky. CO-OPS-sourced results are "
                        "still valid; VDatum fallbacks may fail this run. ***")
        log.info("")
        time.sleep(cfg.sleep_between_calls)

    vd_calls = 0
    vd_errors = 0
    vd_domain = 0
    canary_on_first_error = None

    for i, sr in enumerate(work_rows, 1):
        process_station(session, sr, cfg, recheck=recheck_failures)
        for outcome in sr.vd_outcomes:
            vd_calls += 1
            if outcome == "API_ERROR":
                vd_errors += 1
            elif outcome == "OUT_OF_DOMAIN":
                vd_domain += 1
            if outcome == "API_ERROR" and canary_on_first_error is None and cfg.canary_enabled:
                time.sleep(cfg.sleep_between_calls)
                up, msg = vdatum_canary(session, cfg)
                canary_on_first_error = up
                if up:
                    log.info("VDatum canary (after first API error): UP — %s "
                             "(this failure looks data-specific).", msg)
                else:
                    log.warning("VDatum canary (after first API error): FAILED — %s "
                                "(the service itself looks unhealthy).", msg)

        label = tuple(sr.passthrough.values())[-1] if sr.passthrough else ""
        line = f"{sr.status} [{cfg.tidal_datum}:{sr.tidal_source} {cfg.geodetic_datum}:{sr.geodetic_source or '-'}]"
        log.info("[%3d/%d] %-10s %-42s %s", i, total, sr.station_id, line, label)
        time.sleep(cfg.sleep_between_calls)

    results_path, exc_path = write_outputs(rows, cfg)
    bug_report_path = write_vdatum_bug_report(rows, cfg)

    error_rate = (vd_errors / vd_calls) if vd_calls else 0.0
    canary_end = None
    if cfg.canary_enabled and vd_errors:
        time.sleep(cfg.sleep_between_calls)
        up, msg = vdatum_canary(session, cfg)
        canary_end = up
        log.info("VDatum canary (end of run): %s — %s", "UP" if up else "FAILED", msg)

    # Tally.
    n_results = sum(1 for sr in rows if sr.status == "OK")
    n_exc = sum(1 for sr in rows if sr.is_exception)
    src_counts: dict[str, int] = {}
    for sr in rows:
        if sr.status == "OK":
            src_counts[f"{cfg.geodetic_datum} from {sr.geodetic_source}"] = \
                src_counts.get(f"{cfg.geodetic_datum} from {sr.geodetic_source}", 0) + 1

    log.info("")
    log.info("--- Summary ---")
    log.info("  Results (NAVD88 obtained): %d", n_results)
    for k, n in sorted(src_counts.items()):
        log.info("    %-28s %d", k, n)
    log.info("  Exceptions (no NAVD88):    %d", n_exc)
    if vd_calls:
        log.info("")
        log.info("  VDatum fallback calls: %d  (success %d, out-of-domain %d, "
                 "server/API errors %d = %.0f%%)",
                 vd_calls, vd_calls - vd_errors - vd_domain, vd_domain, vd_errors,
                 error_rate * 100)

    likely_down = (vd_calls > 0
                   and error_rate >= cfg.down_error_rate
                   and (canary_end is False or canary_start is False))
    if likely_down:
        bar = "!" * 72
        log.warning("")
        log.warning(bar)
        log.warning("*** VDatum API is LIKELY DOWN / UNHEALTHY ***")
        log.warning("%.0f%% of VDatum calls returned server/API errors (threshold %.0f%%),",
                    error_rate * 100, cfg.down_error_rate * 100)
        log.warning("and the known-good canary point also failed.")
        log.warning("ACTION: CO-OPS-sourced rows are valid. Re-run later (or use")
        log.warning("        --recheck-failures) to fill VDatum fallbacks;")
        log.warning("        check https://vdatum.noaa.gov/ for status.")
        log.warning(bar)
    elif vd_errors and (canary_end is True or canary_on_first_error is True):
        log.info("")
        log.info("Note: some VDatum calls errored, but the canary succeeded — those")
        log.info("      failures look point-specific (genuinely out of domain or a")
        log.info("      VDatum quirk at those coordinates), not a full outage.")

    log.info("")
    log.info("Results:    %s", results_path)
    log.info("Exceptions: %s", exc_path)
    if bug_report_path:
        log.info("VDatum bug report: %s", bug_report_path)
        log.info("  (persistent VDatum server faults with exact failing URLs —")
        log.info("   forward to the NOAA VDatum Program Support team.)")
    log.info("Log:        %s", logfile_path)

    return likely_down


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Batch transform a tidal datum to a geodetic "
                                            "datum (CO-OPS + NOAA VDatum fallback).")
    p.add_argument("--config", "-c", default="config.ini", help="Path to INI config file.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N stations (for testing).")
    p.add_argument("--recheck-failures", action="store_true",
                   help="Re-run only the stations previously marked NO_GEODETIC in the "
                        "prior output, merging successes back into the results/exceptions "
                        "files. Use when VDatum recovers.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = load_config(args.config)
    try:
        likely_down = run(cfg, limit=args.limit, recheck_failures=args.recheck_failures)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 3 if likely_down else 0


if __name__ == "__main__":
    sys.exit(main())
