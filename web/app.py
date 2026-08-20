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
        d = http_json("https://geocoding-api.open-meteo.com/v1/search"
                      f"?name={q}&count=8&language=en&format=json")
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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
