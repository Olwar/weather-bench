"""Web front end for the weather-bench blend.

Serves two things, and is careful about the difference:

  1. A LIVE forecast for any location, produced by the same unweighted
     multi-model mean (blend_mean) that the benchmark verified as the most
     accurate source at every lead through day 7.
  2. The VERIFICATION STATISTICS behind that claim, read straight out of
     score.py's output so the site cannot quietly disagree with the benchmark.

Foreca is deliberately NOT a live member here. The benchmark scrapes their
public feed to VERIFY against, which is research use; re-serving their
forecast inside a competing forecast product is a different thing entirely.
They appear on the site only as an aggregate skill statistic.

Members are whoever answers for the requested point: the Open-Meteo models
work worldwide, MET Nordic covers the Nordics, and FMI's edited forecast
covers Scandinavia. The blend is the mean of whoever showed up, which is
exactly blend_mean's definition, so the live product and the verified product
are the same estimator.
"""
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common import http_json, fmi_simple, OM_MODELS

DATA_DIR = Path(os.environ.get("WEATHERBENCH_DATA", "/opt/weather-bench/data"))
STATIC = Path(__file__).parent / "static"

OM_VARS = {"temperature_2m": "t2m", "wind_speed_10m": "ws", "precipitation": "rain1h"}
# Display-only extras (conditions/icons). Not verified - the benchmark has no
# cloud observations - so they stay out of the ensemble call and the DB.
OM_EXTRA = {"cloud_cover": "cc", "is_day": "isday"}
OM_ALL = {**OM_VARS, **OM_EXTRA}
AIFS_ENS_MODEL = "ecmwf_aifs025_ensemble"
FMI_FC_PARAMS = {"temperature": "t2m", "windspeedms": "ws", "precipitation1h": "rain1h"}

# Human-facing member names. Keys match the benchmark's source ids so the
# forecast page and the stats page can never drift apart.
MEMBER_LABELS = {
    "ecmwf_aifs025_single": "ECMWF AIFS",
    "ecmwf_aifs_ens_mean": "AIFS ensemble",
    "ecmwf_ifs025": "ECMWF IFS",
    "best_match": "Open-Meteo blend",
    "metno_nordic": "MET Nordic",
    "fmi_edited": "FMI (edited)",
}

CACHE_TTL = 900        # 15 min: Open-Meteo is free, so do not hammer it
_cache: dict = {}

app = FastAPI(title="weather-bench", docs_url=None, redoc_url=None)


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    if len(_cache) > 500:                      # crude bound, this is a small site
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
            _cache.pop(k, None)
    return val


# ---------------------------------------------------------------- members

def _fetch_om_models(lat, lon):
    data = http_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly={','.join(OM_ALL)}&wind_speed_unit=ms&timezone=auto"
        f"&models={','.join(OM_MODELS)}&forecast_days=14"
    )
    blocks = data if isinstance(data, list) else [data]
    out, meta = {}, {}
    for block in blocks:
        hourly = block.get("hourly", {})
        times = hourly.get("time", [])
        meta["utc_offset_seconds"] = block.get("utc_offset_seconds", 0)
        meta["timezone"] = block.get("timezone")
        for key, values in hourly.items():
            if key == "time":
                continue
            model = next((m for m in OM_MODELS if key.endswith("_" + m)), None)
            stem = key[: -(len(model) + 1)] if model else key
            var = OM_ALL.get(stem)
            if model is None or var is None:
                continue
            for t, v in zip(times, values):
                if v is not None:
                    out.setdefault((t, var), {})[model] = v
    return out, meta


def _fetch_om_ensemble(lat, lon):
    data = http_json(
        "https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly={','.join(OM_VARS)}&wind_speed_unit=ms&timezone=auto"
        f"&models={AIFS_ENS_MODEL}&forecast_days=14"
    )
    blocks = data if isinstance(data, list) else [data]
    out = {}
    for block in blocks:
        hourly = block.get("hourly", {})
        times = hourly.get("time", [])
        for om_var, var in OM_VARS.items():
            series = [v for k, v in hourly.items()
                      if k == om_var or k.startswith(om_var + "_member")]
            if not series:
                continue
            for i, t in enumerate(times):
                vals = [s[i] for s in series if i < len(s) and s[i] is not None]
                if vals:
                    out.setdefault((t, var), {})["ecmwf_aifs_ens_mean"] = sum(vals) / len(vals)
    return out


def _fetch_fmi(lat, lon, utc_offset):
    """FMI's human-edited forecast. Scandinavia only - absence is normal, not an
    error, so every failure degrades to 'this member did not show up'."""
    from datetime import datetime, timedelta, timezone as tz
    now = datetime.now(tz.utc)
    rows = fmi_simple(
        "fmi::forecast::edited::weather::scandinavia::point::simple",
        latlon=f"{lat},{lon}", parameters=",".join(FMI_FC_PARAMS), timestep=60,
        starttime=now.strftime("%Y-%m-%dT%H:00:00Z"),
        endtime=(now + timedelta(days=10)).strftime("%Y-%m-%dT%H:00:00Z"),
    )
    out = {}
    for t, p, v in rows:
        var = FMI_FC_PARAMS.get(p)
        if var is None:
            continue
        # FMI answers in UTC; shift onto the same local clock the Open-Meteo
        # members were requested on, else the members would never line up.
        local = datetime.strptime(t, "%Y-%m-%dT%H:%MZ") + timedelta(seconds=utc_offset)
        out.setdefault((local.strftime("%Y-%m-%dT%H:%M"), var), {})["fmi_edited"] = v
    return out


def build_forecast(lat: float, lon: float) -> dict:
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_models = ex.submit(_fetch_om_models, lat, lon)
        f_ens = ex.submit(_fetch_om_ensemble, lat, lon)
        try:
            members, meta = f_models.result()
        except Exception as e:
            raise HTTPException(502, f"upstream model feed failed: {e}")
        try:
            for k, v in f_ens.result().items():
                members.setdefault(k, {}).update(v)
        except Exception:
            pass                                    # ensemble is optional
        try:
            for k, v in _fetch_fmi(lat, lon, meta.get("utc_offset_seconds", 0)).items():
                if k in members:                    # never invent an hour FMI alone has
                    members[k].update(v)
        except Exception:
            pass                                    # outside Scandinavia, or FMI down

    hours: dict = {}
    for (t, var), vals in members.items():
        if len(vals) < 2:                           # a blend needs >= 2 members
            continue
        vs = list(vals.values())
        hours.setdefault(t, {})[var] = {
            "blend": sum(vs) / len(vs),
            "lo": min(vs), "hi": max(vs),
            "members": {MEMBER_LABELS.get(m, m): round(v, 2) for m, v in vals.items()},
        }
    series = [{"time": t, **v} for t, v in sorted(hours.items())]
    present = sorted({m for vals in members.values() for m in vals})
    return {
        "lat": lat, "lon": lon,
        "timezone": meta.get("timezone"),
        "utc_offset_seconds": meta.get("utc_offset_seconds", 0),
        "members": [MEMBER_LABELS.get(m, m) for m in present],
        "hours": series,
    }


# ---------------------------------------------------------------- routes

@app.get("/api/geocode")
def geocode(q: str = Query(min_length=2, max_length=80)):
    def go():
        # quote() is load-bearing: FastAPI hands us the DECODED query, so a
        # space or any non-ASCII letter pasted raw into the upstream URL makes
        # urllib reject the request - "järvelä" and "new york" both 502'd.
        d = http_json("https://geocoding-api.open-meteo.com/v1/search"
                      f"?name={urllib.parse.quote(q)}&count=8&language=fi&format=json")
        return [{"name": r["name"], "country": r.get("country", ""),
                 "admin1": r.get("admin1", ""),
                 "lat": r["latitude"], "lon": r["longitude"]}
                for r in d.get("results", [])]
    try:
        return {"results": _cached(("geo", q.lower()), go)}
    except Exception as e:
        raise HTTPException(502, f"geocoding failed: {e}")


@app.get("/api/forecast")
def forecast(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    key = ("fc", round(lat, 2), round(lon, 2))
    return _cached(key, lambda: build_forecast(round(lat, 4), round(lon, 4)))


@app.get("/api/stats")
def stats():
    """Verification numbers exactly as score.py computed them - the site never
    recomputes or rounds them into something friendlier."""
    f = DATA_DIR / "prospective_results.json"
    if not f.exists():
        raise HTTPException(503, "no scored results yet - run score.py")
    d = json.loads(f.read_text())
    return JSONResponse({
        "hourly_t2m": d.get("hourly_t2m", {}),
        "hourly_ws": d.get("hourly_ws", {}),
        "rain_occurrence": d.get("rain_occurrence", {}),
        "pairwise_blends": d.get("pairwise_t2m_blends_exploratory", {}),
        "pairwise_prereg": d.get("pairwise_t2m", {}),
        "config": d.get("config", {}),
        "generated": f.stat().st_mtime,
        "labels": MEMBER_LABELS,
    })


@app.get("/api/health")
def health():
    return {"ok": True, "stats": (DATA_DIR / "prospective_results.json").exists()}


@app.get("/webmcp.js")
def webmcp_js():
    return FileResponse(STATIC / "webmcp.js", media_type="application/javascript")


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/og.png")
def og_png():
    return FileResponse(STATIC / "og.png", media_type="image/png")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ════════════════════════════════════════════════════════════════════════
# Agent-facing endpoints (WebMCP tools call these). They serve what a human
# cannot read but an agent can compute with: calibrated probabilities from
# the verified error record, forecast churn vs historical norms, error-vs-
# lead curves, and coherent member scenarios. Nightly inputs come from
# agent_stats.py; live members come from the same build_forecast the page
# uses, so the agent and the human always reason about identical data.
# ════════════════════════════════════════════════════════════════════════
import math
import sqlite3
from datetime import datetime, timedelta, timezone

from common import CITIES, DB_PATH

_ASTATS = {"mtime": 0, "data": None}


_CHECK_CACHE = {"t": 0.0, "last": None}


def _check_again():
    """Honest re-ask times from the pipeline's own clocks: the next model
    snapshot lands ~5h after the last stored run, and calibration/verification
    refresh at the nightly rescore. Agents can schedule their own follow-up.

    The naive MAX(run_time) is a full scan of a 30M-row table (run_time is
    third in the PK) and took minutes - it hung every assess call. Scoping to
    one always-collected source rides the index; a 60 s cache absorbs bursts."""
    if time.time() - _CHECK_CACHE["t"] > 60:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        _CHECK_CACHE["last"] = con.execute(
            "SELECT MAX(run_time) FROM forecasts WHERE source='ecmwf_ifs025'"
        ).fetchone()[0]
        _CHECK_CACHE["t"] = time.time()
        con.close()
    last = _CHECK_CACHE["last"]
    nxt = (datetime.strptime(last, "%Y-%m-%dT%H:%MZ") + timedelta(hours=5))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if nxt < now:
        nxt = now + timedelta(minutes=30)
    resc = now.replace(hour=5, minute=45, second=0)
    if resc < now:
        resc += timedelta(days=1)
    return {"next_model_snapshot_utc": nxt.strftime("%Y-%m-%dT%H:%MZ"),
            "next_verification_refresh_utc": resc.strftime("%Y-%m-%dT%H:%MZ")}


def _astats():
    f = DATA_DIR / "agent_stats.json"
    m = f.stat().st_mtime
    if m != _ASTATS["mtime"]:
        _ASTATS.update(mtime=m, data=json.loads(f.read_text()))
    return _ASTATS["data"]


def _results():
    return json.loads((DATA_DIR / "prospective_results.json").read_text())


def _nearest_city(lat, lon):
    best = min(CITIES, key=lambda c: (c["lat"] - lat) ** 2
               + ((c["lon"] - lon) * math.cos(math.radians(lat))) ** 2)
    km = 111.0 * math.sqrt((best["lat"] - lat) ** 2
                           + ((best["lon"] - lon) * math.cos(math.radians(lat))) ** 2)
    return best, round(km)


def _hours_window(lat, lon, start, end):
    fc = _cached(("fc", round(lat, 2), round(lon, 2)),
                 lambda: build_forecast(round(lat, 4), round(lon, 4)))
    off = fc.get("utc_offset_seconds", 0)
    out = []
    for h in fc["hours"]:
        if start <= h["time"] <= end and "t2m" in h:
            utc = datetime.strptime(h["time"], "%Y-%m-%dT%H:%M") - timedelta(seconds=off)
            lead = int((utc.replace(tzinfo=timezone.utc)
                        - datetime.now(timezone.utc)).total_seconds() // 86400)
            out.append((h, max(0, lead)))
    return fc, out


def _sb(sd):
    e = _astats()["spread_edges"]
    return 0 if sd < e[0] else (1 if sd < e[1] else 2)


def _t2m_cdf(lead, sb, x):
    """P(error <= x) by linear interpolation over the empirical quantiles;
    falls back toward lower leads when a cell is thin."""
    q = _astats()["t2m_error_quantiles"]
    cell = None
    for l in range(min(lead, 9), -1, -1):
        cell = q.get(f"{l},{sb}") or cell
        if cell:
            break
    if not cell:
        return None
    pts = [(0.05, cell["q5"]), (0.10, cell["q10"]), (0.25, cell["q25"]),
           (0.50, cell["q50"]), (0.75, cell["q75"]), (0.90, cell["q90"]),
           (0.95, cell["q95"])]
    if x <= pts[0][1]:
        return 0.05
    if x >= pts[-1][1]:
        return 0.95
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if v1 <= x <= v2:
            return p1 + (p2 - p1) * ((x - v1) / (v2 - v1) if v2 > v1 else 0)
    return 0.5


def _rain_p(lead, mm):
    cal = _astats()["rain_calibration"]
    for bi, (lo, hi) in enumerate(_astats()["rain_buckets"]):
        if lo <= mm < hi:
            for l in range(min(lead, 9), -1, -1):
                c = cal.get(f"{l},{bi}")
                if c:
                    return c["p_wet"]
    return None


@app.get("/api/agent/probability")
def agent_probability(lat: float, lon: float, start: str, end: str,
                      lo: float = -99, hi: float = 99):
    """P(lo <= t2m <= hi) per hour, from the verified error distribution
    conditioned on lead and live member spread."""
    _, hours = _hours_window(lat, lon, start, end)
    if not hours:
        raise HTTPException(404, "no forecast hours in window")
    per = []
    for h, lead in hours:
        m = list(h["t2m"]["members"].values())
        sb = _sb((sum((x - sum(m) / len(m)) ** 2 for x in m) / len(m)) ** 0.5)
        blend = h["t2m"]["blend"]
        c_hi = _t2m_cdf(lead, sb, hi - blend)
        c_lo = _t2m_cdf(lead, sb, lo - blend)
        if c_hi is None:
            continue
        per.append({"time": h["time"], "blend": round(blend, 1),
                    "p_in_range": round(max(0.0, c_hi - c_lo), 2)})
    ps = [x["p_in_range"] for x in per]
    return {"hours": per, "p_all_hours_min": min(ps), "p_mean": round(sum(ps) / len(ps), 2),
            "basis": f"calibrated on {_astats()['generated']} verified record",
            "note": "probabilities from empirical error quantiles conditioned on lead day and live model spread"}


@app.get("/api/agent/rain")
def agent_rain(lat: float, lon: float, start: str, end: str):
    """Calibrated P(measurable rain >= 0.1 mm) per hour + window summary."""
    _, hours = _hours_window(lat, lon, start, end)
    per, p_dry = [], 1.0
    for h, lead in hours:
        mm = h.get("rain1h", {}).get("blend", 0.0)
        p = _rain_p(lead, mm)
        if p is None:
            continue
        per.append({"time": h["time"], "forecast_mm": round(mm, 2), "p_rain": p})
        p_dry *= (1 - p)
    if not per:
        raise HTTPException(404, "no rain-calibrated hours in window")
    return {"hours": per, "p_any_rain_upper": round(1 - p_dry, 2),
            "p_rain_max_hour": max(x["p_rain"] for x in per),
            "note": "per-hour probabilities are calibrated frequencies from the verified record; "
                    "the any-rain figure assumes hour independence and is an upper bound"}


@app.get("/api/agent/stability")
def agent_stability(lat: float, lon: float, date: str):
    """How much has the forecast for this date been churning between runs,
    vs historical churn at this lead? Only benchmark cities carry run
    history; the nearest one answers, distance disclosed."""
    city, km = _nearest_city(lat, lon)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT run_time, value FROM forecasts WHERE source='ecmwf_aifs025_single'"
        " AND city=? AND var='t2m' AND target_time LIKE ?",
        (city["key"], f"{date}%")).fetchall()
    con.close()
    runs = {}
    for rt, v in rows:
        runs.setdefault(rt, []).append(v)
    means = sorted((rt, sum(v) / len(v)) for rt, v in runs.items() if len(v) >= 12)[-10:]
    if len(means) < 4:
        raise HTTPException(404, "not enough run history for this date")
    vals = [m for _, m in means]
    mu = sum(vals) / len(vals)
    churn = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
    lead = max(0, (datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                   - datetime.now(timezone.utc)).days)
    norm = _astats()["churn_norms"].get(str(min(lead, 9)), {})
    verdict = ("stable" if churn <= norm.get("p50", 99) else
               "typical churn" if churn <= norm.get("p80", 99) else
               "unusually jumpy - low confidence, consider deciding later")
    return {"check_again_at": _check_again(),
            "station_city": city["key"], "station_distance_km": km,
            "runs_considered": len(means), "daily_mean_by_run":
                [{"run": rt, "t2m_mean": round(m, 1)} for rt, m in means],
            "churn_stddev": round(churn, 2), "historical_norm": norm, "verdict": verdict}


@app.get("/api/agent/decide_by")
def agent_decide_by(lat: float, lon: float, event_date: str):
    """The error-vs-lead curve between now and the event: what waiting buys."""
    city, km = _nearest_city(lat, lon)
    r = _results()
    cc = next(c["country"] for c in CITIES if c["key"] == city["key"])
    boards = r["countries"].get(cc, {}) if cc != "fi" else r
    t2m = boards.get("hourly_t2m", {}).get("blend_open", {})
    rain = boards.get("rain_occurrence", {}).get("blend_open", {})
    lead = max(0, (datetime.strptime(event_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                   - datetime.now(timezone.utc)).days)
    curve = []
    for l in range(lead, -1, -1):
        c = t2m.get(str(l))
        if c:
            curve.append({"decide_days_before": l,
                          "expected_t2m_mae": c["mae"],
                          "rain_csi": rain.get(str(l), {}).get("csi")})
    if not curve:
        raise HTTPException(404, f"no verified lead curve for country {cc} yet")
    return {"country": cc, "verified_at_km": km, "curve": curve,
            "note": "MAE from the nightly-verified record; waiting reduces expected error by the difference between rows"}


@app.get("/api/agent/scenarios")
def agent_scenarios(lat: float, lon: float, start: str, end: str):
    """Coherent per-model scenarios over the window - members are physical
    worlds, so the cold outcome is usually also the wet, windy one."""
    _, hours = _hours_window(lat, lon, start, end)
    if not hours:
        raise HTTPException(404, "no forecast hours in window")
    per = {}
    for h, _ in hours:
        for name, v in h["t2m"]["members"].items():
            d = per.setdefault(name, {"t": [], "r": 0.0, "w": []})
            d["t"].append(v)
        for name, v in h.get("rain1h", {}).get("members", {}).items():
            per.setdefault(name, {"t": [], "r": 0.0, "w": []})["r"] += v
        for name, v in h.get("ws", {}).get("members", {}).items():
            per.setdefault(name, {"t": [], "r": 0.0, "w": []})["w"].append(v)
    scen = []
    for name, d in per.items():
        if not d["t"]:
            continue
        scen.append({"model": name, "t2m_mean": round(sum(d["t"]) / len(d["t"]), 1),
                     "rain_total_mm": round(d["r"], 1),
                     "wind_max_ms": round(max(d["w"]), 1) if d["w"] else None})
    dry = [s for s in scen if s["rain_total_mm"] < 0.5]
    wet = [s for s in scen if s["rain_total_mm"] >= 0.5]
    return {"scenarios": sorted(scen, key=lambda s: s["rain_total_mm"]),
            "dry_share": round(len(dry) / len(scen), 2) if scen else None,
            "groups": {"dry": [s["model"] for s in dry], "wet": [s["model"] for s in wet]},
            "note": "each row is one model's coherent trajectory over the window, not an independent statistic"}


@app.get("/api/agent/best_source")
def agent_best_source(country: str = "fi", var: str = "t2m"):
    """Who is measurably best at each lead, in this country, per the record."""
    r = _results()
    boards = r if country == "fi" else r.get("countries", {}).get(country)
    if not boards:
        raise HTTPException(404, f"no verified record for {country}")
    key = {"t2m": "hourly_t2m", "ws": "hourly_ws", "cc": "hourly_cc",
           "rain": "rain_occurrence"}.get(var)
    board = boards.get(key, {})
    higher = var == "rain"
    metric = "csi" if higher else "mae"
    out = {}
    for lead in range(0, 10):
        cells = [(s, b[str(lead)][metric]) for s, b in board.items()
                 if b.get(str(lead)) and b[str(lead)].get(metric) is not None
                 and (s == "blend_open" or not s.startswith("blend_"))]
        if cells:
            cells.sort(key=lambda x: x[1], reverse=higher)
            out[str(lead)] = [{"source": s, metric: v} for s, v in cells[:3]]
    return {"country": country, "var": var, "best_by_lead": out}


@app.get("/api/agent/assess")
def agent_assess(lat: float, lon: float, start: str, end: str,
                 rain_bad_mm: float = 0.5, wind_bad_ms: float = 10.0,
                 tmin_ok: float = -99, tmax_ok: float = 99,
                 cost_cancel: float = 0, cost_ruined: float = 0):
    """The decision endpoint: P(conditions bad) with per-driver breakdown,
    and expected-value advice when the caller supplies stakes."""
    _, hours = _hours_window(lat, lon, start, end)
    if not hours:
        raise HTTPException(404, "no forecast hours in window")
    p_dry, wind_exceed, temp_probs = 1.0, [], []
    for h, lead in hours:
        mm = h.get("rain1h", {}).get("blend", 0.0)
        p = _rain_p(lead, mm) if mm >= 0.05 else (_rain_p(lead, mm) or 0.02)
        p_dry *= (1 - min(p or 0.02, 0.95)) if rain_bad_mm <= 0.5 else \
                 (1 - min((p or 0.02) * min(1.0, mm / rain_bad_mm), 0.95))
        ws = h.get("ws", {})
        if ws:
            mem = list(ws["members"].values())
            wind_exceed.append(sum(1 for x in mem if x >= wind_bad_ms) / len(mem))
        m = list(h["t2m"]["members"].values())
        sb = _sb((sum((x - sum(m) / len(m)) ** 2 for x in m) / len(m)) ** 0.5)
        blend = h["t2m"]["blend"]
        c_hi = _t2m_cdf(lead, sb, tmax_ok - blend)
        c_lo = _t2m_cdf(lead, sb, tmin_ok - blend)
        if c_hi is not None:
            temp_probs.append(max(0.0, c_hi - c_lo))
    p_rain_bad = round(1 - p_dry, 2)
    p_wind_bad = round(max(wind_exceed), 2) if wind_exceed else 0.0
    p_temp_ok = round(min(temp_probs), 2) if temp_probs else 1.0
    p_bad = round(min(0.97, 1 - (1 - p_rain_bad) * (1 - p_wind_bad) * p_temp_ok), 2)
    out = {"p_bad": p_bad,
           "drivers": {"p_rain_bad": p_rain_bad, "p_wind_bad_any_hour": p_wind_bad,
                       "p_temp_in_ok_range_worst_hour": p_temp_ok},
           "calibration": {"rain": "verified frequencies", "temp": "verified quantiles",
                           "wind": "raw member exceedance (not yet calibrated)"}}
    out["check_again_at"] = _check_again()
    if cost_cancel > 0 and cost_ruined > 0:
        ev_go = p_bad * cost_ruined
        out["decision"] = {"recommend": "cancel" if cost_cancel < ev_go else "go",
                           "expected_loss_if_go": round(ev_go),
                           "cost_if_cancel": cost_cancel,
                           "flips_if_p_bad_crosses": round(cost_cancel / cost_ruined, 2)}
    return out


# ════════════════════════════════════════════════════════════════════════
# The site's own agent ("Kysy Ilmalta") - WebMCP deployment pattern 3:
# an agent HOSTED BY THE SITE, using the very same tool table the browser
# agents use. This endpoint is a thin authenticated proxy to OpenRouter;
# the agentic loop runs in the page, which executes tool calls locally and
# feeds results back. The key never leaves the server; the tools never
# leave the browser.
# ════════════════════════════════════════════════════════════════════════
import urllib.request as _rq

OPENROUTER_KEY_FILE = Path("/opt/weather-bench/openrouter_key.txt")
CHAT_MODEL = os.environ.get("ILMA_CHAT_MODEL", "anthropic/claude-haiku-4.5")
_RATE: dict = {}

CHAT_SYSTEM = (
    "You are Ilma's assistant on ilma.io, a multi-model weather service with a "
    "nightly-verified accuracy record. Answer in the language of the user's "
    "message, honoring the ui-language hint in the context tag. ALWAYS use the provided tools rather than your own weather "
    "knowledge - they return calibrated probabilities, model disagreement, "
    "forecast stability and verification evidence. When discussing a place, "
    "call show_me so the page follows the conversation; use mark_hours to "
    "point at specific windows on the chart. Be concise and concrete: numbers "
    "with their uncertainty, never vibes. If a tool fails, say so honestly."
)


@app.post("/api/agent/chat")
def agent_chat(payload: dict):
    now = time.time()
    ip = "shared"  # behind Vercel; per-IP needs header plumbing - keep a global soft cap
    hits = [t for t in _RATE.get(ip, []) if now - t < 600]
    if len(hits) > 120:
        raise HTTPException(429, "rate limited")
    _RATE[ip] = hits + [now]

    msgs = payload.get("messages") or []
    tools = payload.get("tools") or []
    if not isinstance(msgs, list) or len(msgs) > 24:
        raise HTTPException(400, "bad messages")
    if sum(len(json.dumps(m)) for m in msgs) > 24000:
        raise HTTPException(400, "conversation too long")

    body = json.dumps({
        "model": CHAT_MODEL,
        "max_tokens": 900,
        "temperature": 0.3,
        "messages": [{"role": "system", "content": CHAT_SYSTEM}] + msgs,
        "tools": tools,
    }).encode()
    req = _rq.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY_FILE.read_text().strip()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ilma.io",
            "X-Title": "Ilma",
        },
    )
    try:
        with _rq.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"model call failed: {e}")
    choice = (data.get("choices") or [{}])[0]
    return {"message": choice.get("message"), "finish_reason": choice.get("finish_reason")}
