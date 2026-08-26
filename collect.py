"""Prospective collector: snapshot everyone's live forecast + recent observations.

Run every ~6-12h. Each run stores, per city:
  - Foreca daily forecast (tmin/tmax/rain, ~13 days)    [source=foreca, daily vars]
  - Foreca hourly t2m/ws/rain1h, ~11 days (scraped from the server-rendered
    hour_data blob on foreca.fi /details pages)          [source=foreca, hourly vars]
  - FMI edited (human-curated) hourly t2m/ws/rain1h, ~10 days [source=fmi_edited]
  - Open-Meteo hourly t2m/ws/rain1h per model, 16 days  [source=<model id>]
  - AIFS ensemble MEAN over its 51 members, 16 days     [source=ecmwf_aifs_ens_mean]
  - FMI station observations for the last 166h (verification truth)

run_time = collection hour (UTC). Lead time is computed at scoring from run_time,
which slightly flatters no one in particular: every source is snapshotted at the
same wall-clock moment, exactly like a user opening two weather apps side by side.

Usage: python3 collect.py
"""
import gzip
import re
import shutil
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import (
    CITIES, OM_MODELS, DATA_DIR, get_db, http_get, http_json, fmi_simple,
    fetch_obs, store_observations,
)

HKI = ZoneInfo("Europe/Helsinki")
# FMI edited-forecast parameter -> canonical var
FMI_FC_PARAMS = {"temperature": "t2m", "windspeedms": "ws", "precipitation1h": "rain1h",
                 "humidity": "rh", "dewpoint": "td", "totalcloudcover": "cc",
                 "windgust": "gust", "pressure": "pmsl", "winddirection": "wdir"}
# Open-Meteo hourly variable -> canonical var
OM_VARS = {"temperature_2m": "t2m", "wind_speed_10m": "ws", "precipitation": "rain1h",
           "relative_humidity_2m": "rh", "dew_point_2m": "td", "cloud_cover": "cc",
           "wind_gusts_10m": "gust", "wind_direction_10m": "wdir",
           "pressure_msl": "pmsl", "snow_depth": "snow"}
# Open-Meteo snow_depth is meters, station snow_aws is cm.
OM_SCALE = {"snow": 100.0}
# The all-feeds-dead alarm stays scoped to the original variables: not every
# model publishes every extended field, and a model that lacks cloud cover must
# not take down collection of everything else.
CORE_VARS = {"t2m", "ws", "rain1h"}
# The ensemble endpoint supports fewer fields (its gusts come back null).
ENS_VARS = {k: v for k, v in OM_VARS.items()
            if v in ("t2m", "ws", "rain1h", "rh", "td", "cc", "pmsl")}
# ECMWF AIFS ensemble (separate Open-Meteo endpoint; 51 members incl. control).
# We store the ensemble MEAN - averaging cancels unpredictable detail, so the mean
# normally beats any single deterministic run on MAE. This is the strongest freely
# available AI forecast and the realistic "best a solo dev could ship" candidate.
AIFS_ENS_MODEL = "ecmwf_aifs025_ensemble"
AIFS_ENS_SOURCE = "ecmwf_aifs_ens_mean"


def log(con, run_time, source, city, rows, error=None):
    con.execute(
        "INSERT INTO collect_log(run_time, source, city, rows, error) VALUES(?,?,?,?,?)",
        (run_time, source, city, rows, error),
    )
    if error:
        print(f"  !! {source}/{city}: {error}")


def collect_foreca(con, run_time):
    for city in CITIES:
        try:
            data = http_json(f"https://api.foreca.net/data/daily/{city['foreca_id']}.json")
            rows = []
            for d in data.get("data", []):
                date = d["date"]
                for var, key in (("tmin", "tmin"), ("tmax", "tmax"), ("rain", "rain")):
                    if d.get(key) is not None:
                        rows.append(("foreca", city["key"], run_time, date, var, float(d[key])))
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "foreca", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001 - one source failing must not kill the run
            log(con, run_time, "foreca", city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


def collect_foreca_hourly(con, run_time):
    """Scrape the hour_data JS blob foreca.fi embeds server-side in /details pages.

    Keys are local (Europe/Helsinki) timestamps YYYYMMDDHHMMSS; fields used:
    t = temperature degC, ws = wind m/s, p = precipitation mm/h.
    """
    for city in CITIES:
        try:
            html = http_get(f"https://www.foreca.fi/{city['foreca_path']}/details")
            m = re.search(r"var hour_data = (\{.*?\});", html, re.S)
            if not m:
                raise RuntimeError("hour_data blob not found in page")
            rows = []
            for key, body in re.findall(r"'(\d{14})': \{([^}]*)\}", m.group(1)):
                fields = dict(re.findall(r"(\w+): ('[^']*'|-?[\d.]+)", body))
                # DST fall-back note: the ambiguous 03:00 hour resolves as fold=0;
                # one local hour per year maps imperfectly - accepted.
                local = datetime.strptime(key, "%Y%m%d%H%M%S").replace(tzinfo=HKI)
                t_utc = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                for field, var in (("t", "t2m"), ("ws", "ws"), ("p", "rain1h")):
                    if field in fields and not fields[field].startswith("'"):
                        rows.append(("foreca", city["key"], run_time, t_utc, var, float(fields[field])))
            if not rows:
                raise RuntimeError("hour_data parsed to 0 rows")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "foreca_hourly", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "foreca_hourly", city["key"], 0, str(e))
        time.sleep(0.6)
    con.commit()


def collect_fmi_edited(con, run_time):
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:00:00Z")
    end = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:00:00Z")
    for city in CITIES:
        try:
            rows = fmi_simple(
                "fmi::forecast::edited::weather::scandinavia::point::simple",
                place=city["fmi_place"], parameters=",".join(FMI_FC_PARAMS), timestep=60,
                starttime=start, endtime=end,
            )
            stored = [("fmi_edited", city["key"], run_time, t, FMI_FC_PARAMS[p], v)
                      for (t, p, v) in rows if p in FMI_FC_PARAMS]
            if not stored:
                raise RuntimeError(f"0 rows stored (raw rows: {len(rows)}; param rename?)")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                stored,
            )
            log(con, run_time, "fmi_edited", city["key"], len(stored))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "fmi_edited", city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


def collect_open_meteo(con, run_time):
    models = ",".join(OM_MODELS)
    for city in CITIES:
        try:
            data = http_json(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={city['lat']}&longitude={city['lon']}"
                f"&hourly={','.join(OM_VARS)}&wind_speed_unit=ms"
                f"&models={models}&forecast_days=16"
            )
            blocks = data if isinstance(data, list) else [data]
            per_mv = {(m, v): 0 for m in OM_MODELS for v in OM_VARS.values()}
            n = 0
            for block in blocks:
                hourly = block.get("hourly", {})
                times = [t + "Z" for t in hourly.get("time", [])]
                for key, values in hourly.items():
                    if key == "time":
                        continue
                    model = next((m for m in OM_MODELS if key.endswith("_" + m)), None)
                    stem = key[: -(len(model) + 1)] if model else key
                    if model is None and len(OM_MODELS) == 1:
                        model = OM_MODELS[0]
                    var = OM_VARS.get(stem)
                    if model is None or var is None:
                        continue
                    k = OM_SCALE.get(var, 1.0)
                    rows = [
                        (model, city["key"], run_time, t, var, v * k)
                        for t, v in zip(times, values) if v is not None
                    ]
                    con.executemany(
                        "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                        rows,
                    )
                    per_mv[(model, var)] += len(rows)
                    n += len(rows)
            # A single dead model/var must not be masked by the healthy ones
            # (that is exactly how the GraphCast feed died).
            dead = [f"{m}/{v}" for (m, v), c in per_mv.items() if c == 0 and v in CORE_VARS]
            if dead:
                raise RuntimeError(f"empty model/var feeds: {', '.join(dead)}")
            log(con, run_time, "open_meteo", city["key"], n)
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "open_meteo", city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


# yr.no's published forecast = MET Norway Locationforecast 2.0 ("complete").
# CC BY 4.0, commercial use allowed with attribution - the friendliest
# competitor licence in the whole benchmark. Hourly to ~2.5 days, then
# 6-hourly to ~10 days; instant values at sparse timesteps still score as
# ordinary matched pairs, the boards just carry smaller n at long leads.
YR_VARS = {
    "air_temperature": "t2m", "wind_speed": "ws", "wind_from_direction": "wdir",
    "wind_speed_of_gust": "gust", "relative_humidity": "rh",
    "dew_point_temperature": "td", "cloud_area_fraction": "cc",
    "air_pressure_at_sea_level": "pmsl",
}


def collect_yr(con, run_time):
    for city in CITIES:
        try:
            data = http_json(
                "https://api.met.no/weatherapi/locationforecast/2.0/complete"
                f"?lat={city['lat']}&lon={city['lon']}"
            )
            rows = []
            for step in data["properties"]["timeseries"]:
                t = step["time"][:16] + "Z"
                inst = step["data"].get("instant", {}).get("details", {})
                for k, var in YR_VARS.items():
                    v = inst.get(k)
                    if v is not None:
                        rows.append(("yr", city["key"], run_time, t, var, float(v)))
                # next_1_hours covers [T, T+1h); our rain1h convention is
                # hour-ending, so it lands on T+1h.
                n1 = step["data"].get("next_1_hours", {}).get("details", {})
                p1 = n1.get("precipitation_amount")
                if p1 is not None:
                    end = (datetime.strptime(t, "%Y-%m-%dT%H:%MZ")
                           + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%MZ")
                    rows.append(("yr", city["key"], run_time, end, "rain1h", float(p1)))
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "yr", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "yr", city["key"], 0, str(e))
        time.sleep(0.5)
    con.commit()


GOOGLE_KEY_FILE = Path(__file__).parent / "google_api_key.txt"


def collect_google(con, run_time):
    """Google's Weather API (Maps Platform) - the forecast Pixel/Search users
    see, WeatherNext/MetNet-powered. COMPETITOR ONLY, never a blend member:
    Maps Platform terms bar redistribution and building on their content, so it
    is measured against, exactly like Foreca, but through a licensed API.

    Hourly endpoint pages 24 h at a time out to ~240 h => ~10 calls per city
    per run, ~21k calls/month at the 5 h cadence (free tier 10k, overage
    ~$0.15/1k => ~$1.7/month).

    Timestamp convention: Google gives hour intervals. Instantaneous-ish vars
    are stamped at interval START (matching Open-Meteo's top-of-hour values);
    the qpf accumulation is stamped at interval END (our rain1h is
    hour-ending, matching FMI's r_1h).
    """
    if not GOOGLE_KEY_FILE.exists():
        return  # source not configured - benchmark runs fine without it
    key = GOOGLE_KEY_FILE.read_text().strip()
    for city in CITIES:
        try:
            rows, token, pages = [], None, 0
            while pages < 12:
                url = ("https://weather.googleapis.com/v1/forecast/hours:lookup"
                       f"?key={key}&location.latitude={city['lat']}"
                       f"&location.longitude={city['lon']}&hours=240&pageSize=24")
                if token:
                    url += f"&pageToken={token}"
                data = http_json(url)
                if "error" in data:
                    raise RuntimeError(str(data["error"])[:200])
                for h in data.get("forecastHours", []):
                    start = h["interval"]["startTime"][:16] + "Z"
                    end = h["interval"]["endTime"][:16] + "Z"
                    def num(*path):
                        v = h
                        for k in path:
                            v = v.get(k) if isinstance(v, dict) else None
                            if v is None:
                                return None
                        return v
                    for var, val in (
                        ("t2m", num("temperature", "degrees")),
                        ("ws", num("wind", "speed", "value")),
                        ("gust", num("wind", "gust", "value")),
                        ("wdir", num("wind", "direction", "degrees")),
                        ("rh", h.get("relativeHumidity")),
                        ("td", num("dewPoint", "degrees")),
                        ("cc", h.get("cloudCover")),
                        ("pmsl", num("airPressure", "meanSeaLevelMillibars")),
                    ):
                        if val is None:
                            continue
                        if var in ("ws", "gust"):
                            val = val / 3.6  # km/h -> m/s
                        rows.append(("google_weather", city["key"], run_time, start, var, float(val)))
                    qpf = num("precipitation", "qpf", "quantity")
                    if qpf is not None:
                        rows.append(("google_weather", city["key"], run_time, end, "rain1h", float(qpf)))
                token = data.get("nextPageToken")
                pages += 1
                if not token:
                    break
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "google_weather", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "google_weather", city["key"], 0, str(e))
        time.sleep(0.3)
    con.commit()


def collect_aifs_ensemble(con, run_time):
    """Store the AIFS ensemble mean per hour/variable.

    Response shape: hourly = {time, temperature_2m (control), temperature_2m_member01..NN}.
    The mean is taken over the control plus every member, which is the standard
    ensemble mean and what an app would surface as "the" forecast.
    """
    for city in CITIES:
        try:
            data = http_json(
                "https://ensemble-api.open-meteo.com/v1/ensemble"
                f"?latitude={city['lat']}&longitude={city['lon']}"
                f"&hourly={','.join(ENS_VARS)}&wind_speed_unit=ms"
                f"&models={AIFS_ENS_MODEL}&forecast_days=16"
            )
            blocks = data if isinstance(data, list) else [data]
            n = 0
            for block in blocks:
                hourly = block.get("hourly", {})
                times = [t + "Z" for t in hourly.get("time", [])]
                for om_var, var in ENS_VARS.items():
                    # control key is the bare name; members are <name>_memberNN
                    series = [
                        vals for key, vals in hourly.items()
                        if key == om_var or key.startswith(om_var + "_member")
                    ]
                    if not series:
                        continue
                    rows = []
                    for i, t in enumerate(times):
                        vals = [s[i] for s in series if i < len(s) and s[i] is not None]
                        if vals:
                            rows.append((AIFS_ENS_SOURCE, city["key"], run_time, t, var,
                                         sum(vals) / len(vals)))
                    con.executemany(
                        "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                        rows,
                    )
                    n += len(rows)
            if n == 0:
                raise RuntimeError(f"0 rows parsed (error or key change: {str(data)[:150]})")
            log(con, run_time, AIFS_ENS_SOURCE, city["key"], n)
        except Exception as e:  # noqa: BLE001
            log(con, run_time, AIFS_ENS_SOURCE, city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


def collect_obs(con, run_time):
    # 166h window (just under FMI's 168h interval cap): a session outage of up to
    # ~6 days backfills itself on the next run instead of leaving a permanent hole.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=166)).strftime("%Y-%m-%dT%H:00:00Z")
    end = now.strftime("%Y-%m-%dT%H:00:00Z")
    for city in CITIES:
        try:
            rows = fetch_obs(city, start, end)
            if not rows:
                raise RuntimeError("0 rows parsed")
            store_observations(con, city["key"], rows)
            log(con, run_time, "obs", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "obs", city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


def main():
    # Minute precision: exact leads, no elapsed-hour leakage into lead 0, and two
    # runs in the same hour stay distinct snapshots instead of merging.
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    con = get_db()
    print(f"collect run {run_time}")
    collect_foreca(con, run_time)
    collect_foreca_hourly(con, run_time)
    collect_fmi_edited(con, run_time)
    collect_open_meteo(con, run_time)
    collect_aifs_ensemble(con, run_time)
    collect_google(con, run_time)
    collect_yr(con, run_time)
    collect_obs(con, run_time)
    con.commit()
    errs = con.execute(
        "SELECT count(*) FROM collect_log WHERE run_time=? AND error IS NOT NULL", (run_time,)
    ).fetchone()[0]
    total = con.execute(
        "SELECT coalesce(sum(rows),0) FROM collect_log WHERE run_time=?", (run_time,)
    ).fetchone()[0]
    backup_daily(con)
    print(f"done: {total} rows stored, {errs} errors")
    con.close()
    if errs:
        sys.exit(1)  # loud failure so the scheduler/session notices


def backup_daily(con):
    """One consistent DB copy per UTC day, keep the last 7 - the accrued
    competitor snapshots are unrecoverable if the live file is lost.

    Stored gzipped. The live DB passed 2.5 GB at ~4 weeks and grows ~60 MB a
    day, so seven raw copies would have filled the host's disk within weeks and
    taken the unrelated services on that box down with it. Compression puts a
    copy at ~13% of the raw size, which keeps a week of history affordable.
    """
    bdir = DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = bdir / f"bench-{stamp}.sqlite.gz"
    if path.exists():
        return
    con.commit()
    # VACUUM INTO first (it needs a real file), then compress and drop the raw
    # copy, so peak extra disk is one uncompressed DB rather than seven.
    tmp = bdir / f"bench-{stamp}.tmp"
    tmp.unlink(missing_ok=True)
    con.execute(f"VACUUM INTO '{tmp}'")
    with open(tmp, "rb") as src, gzip.open(path, "wb", compresslevel=1) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    tmp.unlink()
    for old in sorted(bdir.glob("bench-*.sqlite.gz"))[:-7]:
        old.unlink()


if __name__ == "__main__":
    main()
