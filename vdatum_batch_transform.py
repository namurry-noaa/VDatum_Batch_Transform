#!/usr/bin/env python3
"""
VDatum Batch Transform
======================

Batch-transform tidal datums to a geodetic datum for a list of NOAA CO-OPS
tide stations.

Pipeline (per station)
-----------------------
1. Pull the station's published datums from the CO-OPS Metadata API (MDAPI).
2. Read the tidal datum value (e.g. MLLW) relative to Station Datum (STND).
      - If absent -> record NULL, HALT that station (nothing else to reference).
3. Read the geodetic datum value (e.g. NAVD88) relative to Station Datum,
   then express it relative to the tidal zero-plane:
      geodetic_rel_tidal = geodetic(STND) - tidal(STND)
      - If present  -> store it (source = "COOPS").
      - If absent   -> fall back to the NOAA VDatum API:
            transform (tidal_datum, z=0) at the station lat/lon -> geodetic.
            VDatum reports the geodetic height of the tidal=0 surface (its
            convention: up is negative). We store BOTH the raw VDatum value
            and the T&C-convention (sign-flipped) value.
            - If the point is outside VDatum's domain (-999999 / error),
              store NULL and continue to the next station.

Outputs (CSV, meters or feet per config) into the configured output dir:
  <basename>.csv            all stations + computed columns + provenance
  <basename>_exceptions.csv only stations that produced a NULL / needed review

The source spreadsheet is treated as READ-ONLY and is never modified.

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

# Module logger. Handlers are attached in setup_logging(); until then, calls
# are silently dropped (NullHandler) so importing the module is side-effect-free.
log = logging.getLogger("vdatum_batch_transform")
log.addHandler(logging.NullHandler())


def setup_logging(logfile_path: str, verbose: bool = True) -> None:
    """Attach a file handler (full detail) and a console handler (summary).

    Every API request/response is logged to the file for reproducibility;
    the console stays readable.
    """
    log.setLevel(logging.DEBUG)
    # Clear any handlers from a previous run (e.g. repeated calls in a session).
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
    tidal_datum: str            # single tidal zero-plane (CO-OPS abbr)
    geodetic_datums: list[str]  # one or more geodetic targets (CO-OPS abbr)
    # vdatum
    s_h_frame: str
    epoch_in: str
    epoch_out: str
    s_v_frame: str
    geoid: str
    region: str
    region_try_order: list[str]
    # api
    application: str
    sleep_between_calls: float
    timeout: float
    max_retries: int
    vdatum_server_retries: int
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


def _split(csv_str: str) -> list[str]:
    return [s.strip() for s in csv_str.split(",") if s.strip()]


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not cp.read(path):
        raise FileNotFoundError(f"Config file not found: {path}")

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
        tidal_datum=cp.get("datums", "tidal_datum").strip().upper(),
        geodetic_datums=[d.upper() for d in _split(
            cp.get("datums", "geodetic_datums",
                   fallback=cp.get("datums", "geodetic_datum", fallback="NAVD88")))],
        s_h_frame=cp.get("vdatum", "s_h_frame"),
        epoch_in=cp.get("vdatum", "epoch_in"),
        epoch_out=cp.get("vdatum", "epoch_out"),
        s_v_frame=cp.get("vdatum", "s_v_frame"),
        geoid=cp.get("vdatum", "geoid"),
        region=cp.get("vdatum", "region", fallback="auto").strip().lower(),
        region_try_order=_split(cp.get("vdatum", "region_try_order", fallback="contiguous")),
        application=cp.get("api", "application", fallback="CO-OPS_VDatum_Batch_Transform"),
        sleep_between_calls=cp.getfloat("api", "sleep_between_calls", fallback=0.5),
        timeout=cp.getfloat("api", "timeout", fallback=30.0),
        max_retries=cp.getint("api", "max_retries", fallback=3),
        vdatum_server_retries=cp.getint("api", "vdatum_server_retries", fallback=3),
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

    Returns an empty dict if the station has no published datums.
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
    # failure_kind distinguishes an honest "outside the tidal grid" result from
    # a server-side/API problem so the caller can set a precise status.
    #   ""                -> success (ok=True)
    #   "OUT_OF_DOMAIN"   -> every applicable region was -999999 or a definitive
    #                        geographic rejection (point simply isn't covered)
    #   "API_ERROR"       -> a persistent "Uncaught error" SERVER fault or an
    #                        HTTP/transport error (a real problem, not geography)
    failure_kind: str = ""
    message: str = ""
    # Persistent server/transport faults captured for the bug report (list of
    # dicts: region, s_h_frame, kind, message, url).
    server_faults: list = field(default_factory=list)


def _classify_vdatum_message(msg: str) -> str:
    """Categorize a VDatum error 'message' string.

    Returns one of:
      "GEOGRAPHIC"  -> region genuinely doesn't contain the point / wrong coast
                       ("Input Region is not correct!", "should be IGS14 for
                       Tidal", "only cover ...", "Unsupported vertical datum").
                       Trying this region again or with other frames is futile;
                       it just means this region is wrong for these coords.
      "SERVER"      -> the generic "Uncaught error, please contact NOAA VDatum
                       Program Support team." fault (possibly transient).
      "OTHER"       -> anything else.
    """
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


def vdatum_convert(session: requests.Session, lat: float, lon: float,
                   t_v_frame: str, cfg: Config) -> VdatumResult:
    """Transform (tidal zero-plane, z=0) -> geodetic (t_v_frame) at (lat, lon).

    Region fallthrough is "smart":
      * A clean -999999 or a definitive GEOGRAPHIC rejection for a region means
        that region simply doesn't apply here -> quietly move to the next region
        (recorded at DEBUG, not surfaced as an error).
      * The generic "Uncaught error" SERVER fault is retried a few times with
        backoff (it is sometimes transient); if it persists it is captured as a
        genuine VDatum server bug and surfaced for reporting.

    Failure classification for the caller:
      * OUT_OF_DOMAIN -> every applicable region was a clean -999999 / geographic
        miss (the point is simply not in any VDatum tidal grid we tried).
      * API_ERROR     -> at least one region hit a persistent SERVER fault or an
        HTTP/transport error (something is wrong beyond geography).
    """
    regions = cfg.region_try_order if cfg.region == "auto" else [cfg.region]
    saw_server_error = False
    saw_out_of_domain_or_geographic = False
    attempts: list[str] = []            # concise, human-facing (Note field)
    server_faults: list[dict] = []      # for the bug report (persistent SERVER)

    for region_spec in regions:
        # Optional "region:FRAME" override (some regional tidal grids require a
        # specific input horizontal frame, e.g. chesapeak_delaware -> IGS14).
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
            "s_v_frame": cfg.s_v_frame,
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

        # --- issue the request, retrying only the transient SERVER fault ---
        data = None
        transport_err = None
        server_msg = None
        for server_try in range(1, cfg.vdatum_server_retries + 1):
            try:
                data = _get_json(session, VDATUM_URL, params, cfg)
            except ApiError as e:
                transport_err = str(e)
                data = None
                break  # transport errors already retried inside _get_json

            msg = data.get("message")
            has_error = data.get("errorCode") or (msg and data.get("t_z") is None)
            if has_error and _classify_vdatum_message(msg) == "SERVER":
                server_msg = msg
                if server_try < cfg.vdatum_server_retries:
                    backoff = cfg.sleep_between_calls * (2 ** (server_try - 1))
                    log.debug("  VDatum %s -> SERVER fault (try %d/%d), backoff %.1fs",
                              tag, server_try, cfg.vdatum_server_retries, backoff)
                    time.sleep(backoff)
                    continue
            break  # got a usable response, or a non-SERVER error, or last try

        # --- interpret the (final) response for this region ---
        if data is None:  # transport/HTTP failure after retries
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
            else:  # GEOGRAPHIC or OTHER -> this region just doesn't apply here
                saw_out_of_domain_or_geographic = True
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

        if t_z <= VDATUM_NODATA + 1:  # -999999 no-data (outside domain / masked)
            saw_out_of_domain_or_geographic = True
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

    # Nothing succeeded. A SERVER/transport fault is a "harder" failure than a
    # clean out-of-domain/geographic miss, so it takes precedence.
    kind = "API_ERROR" if saw_server_error else "OUT_OF_DOMAIN"
    detail = "; ".join(attempts) if attempts else "no region succeeded"
    return VdatumResult(ok=False, failure_kind=kind, message=detail,
                        server_faults=server_faults)


def _vdatum_url(params: dict) -> str:
    """Reconstruct the full VDatum GET URL for logging / bug reports."""
    from urllib.parse import urlencode
    return f"{VDATUM_URL}?{urlencode(params)}"


def vdatum_canary(session: requests.Session, cfg: Config) -> tuple[bool, str]:
    """Probe VDatum with a known-good point to check the service is alive.

    Returns (is_up, message). A True result means VDatum returned a real
    numeric transform for a point that should always succeed; a False result
    means the service errored, timed out, or returned no-data for the canary
    (strong signal the API itself is cranky/down, not that our data is bad).
    """
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
class TargetResult:
    """Per geodetic-target outcome for one station."""
    datum: str = ""                             # e.g. NAVD88
    st_geodetic: Optional[float] = None         # geodetic rel tidal, from COOPS
    st_geodetic_vdatum: Optional[float] = None  # from VDatum, T&C sign (up +)
    vdatum_raw: Optional[float] = None          # VDatum native value (up -)
    vdatum_uncertainty: Optional[float] = None
    vdatum_region: Optional[str] = None
    geodetic_source: str = ""                   # COOPS | VDATUM | (blank)
    status: str = ""                            # OK | VDATUM_FALLBACK |
                                                # VDATUM_OUT_OF_DOMAIN | VDATUM_API_ERROR
    note: str = ""
    server_faults: list = field(default_factory=list)


@dataclass
class StationRow:
    passthrough: dict = field(default_factory=dict)
    station_id: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    st_tidal: Optional[float] = None            # ST_MLLW  (tidal rel STND)
    # station-level status: NO_TIDAL / COOPS_ERROR / PROCESSED
    status: str = ""
    note: str = ""
    targets: dict = field(default_factory=dict)  # {datum: TargetResult}

    @property
    def all_server_faults(self) -> list:
        out = []
        for t in self.targets.values():
            out.extend(t.server_faults)
        return out

    @property
    def is_exception(self) -> bool:
        """True if the station needs review (no tidal, or any target not OK)."""
        if self.status in ("NO_TIDAL", "COOPS_ERROR"):
            return True
        return any(t.status != "OK" for t in self.targets.values())


def process_station(session: requests.Session, sr: StationRow, cfg: Config,
                    only_targets: Optional[list[str]] = None) -> list[str]:
    """Populate sr in place. Returns a list of VDatum-call outcome tags
    ("OK"/"OUT_OF_DOMAIN"/"API_ERROR") for run-level health tracking.

    only_targets: if given, process just those geodetic datums (used by
    --recheck-failures to re-run only the ones that previously failed).
    """
    targets = only_targets if only_targets else cfg.geodetic_datums
    outcomes: list[str] = []

    try:
        datums = fetch_coops_datums(session, sr.station_id, cfg)
    except ApiError as e:
        sr.status = "COOPS_ERROR"
        sr.note = f"CO-OPS datums fetch failed: {e}"
        log.warning("Station %s: COOPS_ERROR: %s", sr.station_id, e)
        return outcomes

    tidal = datums.get(cfg.tidal_datum)
    if tidal is None:
        # No tidal datum -> nothing to reference; halt this station.
        sr.st_tidal = None
        sr.status = "NO_TIDAL"
        sr.note = f"CO-OPS has no {cfg.tidal_datum} datum for this station."
        return outcomes

    sr.st_tidal = tidal
    sr.status = "PROCESSED"

    for geodetic_datum in targets:
        tr = sr.targets.get(geodetic_datum) or TargetResult(datum=geodetic_datum)
        sr.targets[geodetic_datum] = tr
        # reset per-target result (in case of recheck)
        tr.st_geodetic = tr.st_geodetic_vdatum = tr.vdatum_raw = None
        tr.vdatum_uncertainty = tr.vdatum_region = None
        tr.geodetic_source = tr.status = tr.note = ""
        tr.server_faults = []

        geodetic_stnd = datums.get(geodetic_datum)
        if geodetic_stnd is not None:
            # geodetic value expressed relative to the tidal zero-plane
            tr.st_geodetic = round(geodetic_stnd - tidal, 4)
            tr.geodetic_source = "COOPS"
            tr.status = "OK"
            continue

        # Fallback: VDatum. Need coordinates.
        if sr.latitude is None or sr.longitude is None:
            tr.status = "VDATUM_API_ERROR"
            tr.geodetic_source = "VDATUM"
            tr.note = f"CO-OPS lacks {geodetic_datum}; VDatum fallback needs lat/lon (missing)."
            log.warning("Station %s [%s]: missing lat/lon for VDatum fallback",
                        sr.station_id, geodetic_datum)
            continue

        time.sleep(cfg.sleep_between_calls)
        vr = vdatum_convert(session, sr.latitude, sr.longitude, geodetic_datum, cfg)
        if not vr.ok:
            tr.geodetic_source = "VDATUM"
            tr.server_faults = vr.server_faults
            if vr.failure_kind == "OUT_OF_DOMAIN":
                tr.status = "VDATUM_OUT_OF_DOMAIN"
                tr.note = (f"CO-OPS lacks {geodetic_datum}; point outside VDatum tidal "
                           f"domain. {vr.message}")
                outcomes.append("OUT_OF_DOMAIN")
            else:  # API_ERROR
                tr.status = "VDATUM_API_ERROR"
                tr.note = (f"CO-OPS lacks {geodetic_datum}; VDatum API/server error. "
                           f"{vr.message}")
                outcomes.append("API_ERROR")
            short = vr.message.split(";")[0].strip()
            if len(vr.message) > len(short):
                short += "  (…see Note/log for all regions tried)"
            log.warning("Station %s [%s]: %s: %s",
                        sr.station_id, geodetic_datum, tr.status, short)
            continue

        # VDatum t_z = geodetic height of the tidal=0 surface (up-is-negative).
        # T&C convention (geodetic relative to tidal, up-is-positive) = -t_z.
        tr.vdatum_raw = round(vr.t_z, 4)
        tr.st_geodetic_vdatum = round(-vr.t_z, 4)
        tr.vdatum_uncertainty = vr.uncertainty
        tr.vdatum_region = vr.region
        tr.geodetic_source = "VDATUM"
        tr.status = "VDATUM_FALLBACK"
        outcomes.append("OK")

    return outcomes


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def read_input(cfg: Config) -> pd.DataFrame:
    """Load the source table. Accepts .csv, .ods, or .xlsx.

    Required columns (names configurable in [input]): station id, latitude,
    longitude. Optional pass-through columns are carried into the output.
    """
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

    # Friendly validation of the required columns.
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

    # Warn (don't fail) if a configured pass-through column isn't present.
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
        # normalize station id (strip trailing .0 from numeric ids)
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


def write_outputs(rows: list[StationRow], cfg: Config) -> tuple[str, str]:
    os.makedirs(cfg.out_dir, exist_ok=True)
    unit_label = "m" if cfg.units == "metric" else "ft"

    tidal_col = f"ST_{cfg.tidal_datum}"

    # Base (station-level) columns, then a block of columns per geodetic target.
    base_header = (
        cfg.passthrough_cols
        + ["Station ID", "Latitude", "Longitude",
           f"{tidal_col} ({unit_label})"]
    )

    def target_cols(datum: str) -> list[str]:
        return [
            f"ST_{datum} ({unit_label})",
            f"ST_{datum}_VDatum ({unit_label})",
            f"{datum}_VDatum_raw ({unit_label})",
            f"{datum}_VDatum_uncertainty ({unit_label})",
            f"{datum}_VDatum_region",
            f"{datum}_Geodetic_source",
            f"{datum}_Status",
            f"{datum}_Note",
        ]

    header = list(base_header)
    for datum in cfg.geodetic_datums:
        header += target_cols(datum)
    # A station-level status/note summarizing the row.
    header += ["Station_status", "Station_note"]

    def row_to_record(sr: StationRow) -> list:
        rec = [sr.passthrough.get(c, "") for c in cfg.passthrough_cols]
        rec += [sr.station_id, sr.latitude, sr.longitude, sr.st_tidal]
        for datum in cfg.geodetic_datums:
            tr = sr.targets.get(datum)
            if tr is None:
                rec += ["", "", "", "", "", "", "", ""]
            else:
                rec += [tr.st_geodetic, tr.st_geodetic_vdatum, tr.vdatum_raw,
                        tr.vdatum_uncertainty, tr.vdatum_region,
                        tr.geodetic_source, tr.status, tr.note]
        rec += [sr.status, sr.note]
        return rec

    results_path = os.path.join(cfg.out_dir, f"{cfg.basename}.csv")
    exc_path = os.path.join(cfg.out_dir, f"{cfg.basename}_exceptions.csv")

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sr in rows:
            w.writerow(row_to_record(sr))

    exceptions = [sr for sr in rows if sr.is_exception]
    with open(exc_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sr in exceptions:
            w.writerow(row_to_record(sr))

    return results_path, exc_path


def write_vdatum_bug_report(rows: list[StationRow], cfg: Config) -> Optional[str]:
    """Write a report of persistent VDatum SERVER/transport faults, one row per
    (station, target, region attempt), including the exact failing URL —
    suitable to forward to the NOAA VDatum Program Support team.

    Returns the path, or None if there were no server faults.
    """
    fault_rows: list[dict] = []
    for sr in rows:
        for datum, tr in sr.targets.items():
            for f in tr.server_faults:
                fault_rows.append({
                    "Station ID": sr.station_id,
                    "Station Name": sr.passthrough.get("Station Name", ""),
                    "Latitude": sr.latitude,
                    "Longitude": sr.longitude,
                    "Target datum": datum,
                    "Region": f.get("region", ""),
                    "s_h_frame": f.get("s_h_frame", ""),
                    "Fault kind": f.get("kind", ""),
                    "VDatum message": f.get("message", ""),
                    "Failing URL": f.get("url", ""),
                })
    if not fault_rows:
        return None

    path = os.path.join(cfg.out_dir, f"{cfg.basename}_vdatum_bug_report.csv")
    cols = ["Station ID", "Station Name", "Latitude", "Longitude", "Target datum",
            "Region", "s_h_frame", "Fault kind", "VDatum message", "Failing URL"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(fault_rows)
    return path


def _load_prior_results(cfg: Config) -> dict[str, dict]:
    """Read a prior results CSV into {station_id: row_dict} for recheck merges."""
    path = os.path.join(cfg.out_dir, f"{cfg.basename}.csv")
    prior: dict[str, dict] = {}
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"--recheck-failures needs a prior results file: {path} (run a normal pass first).")
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid = (r.get("Station ID") or "").strip()
            if sid:
                prior[sid] = r
    return prior


def _rows_from_prior(prior: dict[str, dict], cfg: Config) -> list[StationRow]:
    """Reconstruct StationRow objects from a prior results CSV so a recheck can
    update them in place and re-emit the full merged output."""
    unit_label = "m" if cfg.units == "metric" else "ft"
    tidal_key = f"ST_{cfg.tidal_datum} ({unit_label})"

    def fnum(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    rows: list[StationRow] = []
    for sid, r in prior.items():
        sr = StationRow(
            passthrough={c: r.get(c, "") for c in cfg.passthrough_cols},
            station_id=sid,
            latitude=fnum(r.get("Latitude")),
            longitude=fnum(r.get("Longitude")),
            st_tidal=fnum(r.get(tidal_key)),
            status=r.get("Station_status", "") or "PROCESSED",
            note=r.get("Station_note", ""),
        )
        for datum in cfg.geodetic_datums:
            tr = TargetResult(
                datum=datum,
                st_geodetic=fnum(r.get(f"ST_{datum} ({unit_label})")),
                st_geodetic_vdatum=fnum(r.get(f"ST_{datum}_VDatum ({unit_label})")),
                vdatum_raw=fnum(r.get(f"{datum}_VDatum_raw ({unit_label})")),
                vdatum_uncertainty=fnum(r.get(f"{datum}_VDatum_uncertainty ({unit_label})")),
                vdatum_region=r.get(f"{datum}_VDatum_region", "") or None,
                geodetic_source=r.get(f"{datum}_Geodetic_source", ""),
                status=r.get(f"{datum}_Status", ""),
                note=r.get(f"{datum}_Note", ""),
            )
            sr.targets[datum] = tr
        rows.append(sr)
    return rows


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(cfg: Config, limit: Optional[int] = None, recheck_failures: bool = False) -> bool:
    """Execute the batch. Returns True if VDatum was judged LIKELY DOWN.

    recheck_failures: instead of processing every station, load the prior
    results CSV and re-run ONLY the (station, target) pairs previously marked
    VDATUM_API_ERROR, merging any new successes back into the same output files.
    """
    os.makedirs(cfg.out_dir, exist_ok=True)
    logfile_path = os.path.join(cfg.out_dir, f"{cfg.basename}.log")
    setup_logging(logfile_path)

    if recheck_failures:
        prior = _load_prior_results(cfg)
        all_rows = _rows_from_prior(prior, cfg)
        # Only rows with at least one API_ERROR target need rechecking.
        recheck_map = {
            sr.station_id: [d for d, tr in sr.targets.items()
                            if tr.status == "VDATUM_API_ERROR"]
            for sr in all_rows
        }
        recheck_map = {sid: ds for sid, ds in recheck_map.items() if ds}
        work_rows = [sr for sr in all_rows if sr.station_id in recheck_map]
        rows = all_rows  # full set is written back out
        mode_desc = (f"RECHECK: {len(work_rows)} station(s) with prior "
                     f"VDATUM_API_ERROR target(s)")
    else:
        df = read_input(cfg)
        rows = build_rows(df, cfg)
        if limit:
            rows = rows[:limit]
        work_rows = rows
        recheck_map = None
        mode_desc = f"FULL: {len(rows)} station(s)"

    total = len(work_rows)
    log.info("VDatum Batch Transform  (log: %s)", logfile_path)
    log.info("Mode: %s", mode_desc)
    log.info("Input: %s [%s]", cfg.in_file, cfg.sheet)
    log.info("Tidal (zero-plane) datum: %s | Geodetic target(s): %s",
             cfg.tidal_datum, ", ".join(cfg.geodetic_datums))
    log.info("Units: %s | VDatum source frame: %s | region: %s",
             cfg.units, cfg.s_v_frame, cfg.region)
    log.info("Region try order: %s", ", ".join(cfg.region_try_order))
    log.info("")

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.application})

    # --- Startup canary: is VDatum even reachable/healthy right now? ---
    canary_start = None
    if cfg.canary_enabled:
        up, msg = vdatum_canary(session, cfg)
        canary_start = up
        if up:
            log.info("VDatum canary (startup): UP — %s", msg)
        else:
            log.warning("VDatum canary (startup): FAILED — %s", msg)
            log.warning("*** VDatum may be down or cranky. T&C ('OK') results are still "
                        "valid; VDatum fallbacks may all fail this run. ***")
        log.info("")
        time.sleep(cfg.sleep_between_calls)

    vd_calls = 0        # VDatum transform attempts (target-level)
    vd_errors = 0       # of those, server/API errors
    vd_domain = 0       # of those, clean out-of-domain
    canary_on_first_error = None

    for i, sr in enumerate(work_rows, 1):
        only = recheck_map.get(sr.station_id) if recheck_map is not None else None
        outcomes = process_station(session, sr, cfg, only_targets=only)
        for outcome in outcomes:
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
        tgt_status = ",".join(f"{d}={sr.targets[d].status}" for d in sr.targets) or sr.status
        log.info("[%3d/%d] %-10s %-30s %s", i, total, sr.station_id, tgt_status, label)
        time.sleep(cfg.sleep_between_calls)

    results_path, exc_path = write_outputs(rows, cfg)
    bug_report_path = write_vdatum_bug_report(rows, cfg)

    # --- End-of-run health verdict ---
    error_rate = (vd_errors / vd_calls) if vd_calls else 0.0
    canary_end = None
    if cfg.canary_enabled and vd_errors:
        time.sleep(cfg.sleep_between_calls)
        up, msg = vdatum_canary(session, cfg)
        canary_end = up
        log.info("VDatum canary (end of run): %s — %s", "UP" if up else "FAILED", msg)

    # Tally final per-target statuses across the full output.
    status_counts: dict[str, int] = {}
    for sr in rows:
        if sr.status in ("NO_TIDAL", "COOPS_ERROR"):
            status_counts[sr.status] = status_counts.get(sr.status, 0) + 1
            continue
        for tr in sr.targets.values():
            key = f"{tr.datum}:{tr.status}"
            status_counts[key] = status_counts.get(key, 0) + 1

    log.info("")
    log.info("--- Summary (per-target status) ---")
    for status, n in sorted(status_counts.items()):
        log.info("  %-28s %d", status, n)
    if vd_calls:
        log.info("")
        log.info("  VDatum calls: %d  (success %d, out-of-domain %d, server/API errors %d = %.0f%%)",
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
        log.warning("ACTION: T&C ('OK') rows are valid. Re-run later (or use")
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
    p = argparse.ArgumentParser(description="Batch transform tidal datums to a geodetic "
                                            "datum (CO-OPS T&C + NOAA VDatum fallback).")
    p.add_argument("--config", "-c", default="config.ini", help="Path to INI config file.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N stations (for testing).")
    p.add_argument("--recheck-failures", action="store_true",
                   help="Re-run only the (station, target) pairs previously marked "
                        "VDATUM_API_ERROR in the prior results CSV, merging successes "
                        "back into the same output files. Use when VDatum recovers.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = load_config(args.config)
    try:
        likely_down = run(cfg, limit=args.limit, recheck_failures=args.recheck_failures)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    # Exit code 3 signals "completed, but VDatum looked down" for scripting/cron;
    # 0 otherwise. (Output files are still written regardless.)
    return 3 if likely_down else 0


if __name__ == "__main__":
    sys.exit(main())
