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
# MET Nordic is a Nordic-only dataset; an empty feed there is geography, not
# an outage, and must not fail collection for Berlin or Houston.
METNO_COUNTRIES = {"fi", "se", "dk", "no"}
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
        if city.get("country") != "fi":
            continue
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
        if city.get("country") != "fi":
            continue
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
        if city.get("country") != "fi":
            continue
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
            dead = [f"{m}/{v}" for (m, v), c in per_mv.items()
                    if c == 0 and v in CORE_VARS
                    and not (m == "metno_nordic" and city.get("country") not in METNO_COUNTRIES)]
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
            # patient retry: the ensemble endpoint is credit-heavy and its
            # minutely cap resets within a minute - a 20 s backoff outlives it
            data = http_json(
                "https://ensemble-api.open-meteo.com/v1/ensemble"
                f"?latitude={city['lat']}&longitude={city['lon']}"
                f"&hourly={','.join(ENS_VARS)}&wind_speed_unit=ms"
                f"&models={AIFS_ENS_MODEL}&forecast_days=16",
                tries=4, sleep=20,
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
            time.sleep(1.5)  # ensemble calls are credit-heavy; 32 cities tripped the minutely cap
        except Exception as e:  # noqa: BLE001
            log(con, run_time, AIFS_ENS_SOURCE, city["key"], 0, str(e))
        time.sleep(0.4)
    con.commit()


METAR_OCTAS = {"CLR": 0, "SKC": 0, "CAVOK": 0, "NCD": 0, "NSC": 0,
               "FEW": 2, "SCT": 4, "BKN": 6, "OVC": 8, "VV": 8}
COMPASS = {d: i * 22.5 for i, d in enumerate(
    ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"])}


def _rh_from_t_td(t, td):
    """Magnus formula; METAR gives T and Td but not RH."""
    import math
    return 100.0 * math.exp(17.625 * td / (243.04 + td)) / math.exp(17.625 * t / (243.04 + t))


def collect_metar_obs(con, run_time):
    """Observation truth for every non-Finnish city: airport METARs via
    aviationweather.gov (NOAA, public domain, no key, global). One call for all
    stations, 72 h back so QC'd/late reports self-heal like the FMI window.

    METARs land at station-specific minutes (:20, :50...), our grid is
    top-of-hour: each report is bucketed to its nearest hour and the report
    closest to the hour wins. Pressure prefers true SLP, falls back to QNH
    (equal to SLP to within ~1 hPa at these low-elevation airports; KDEN is
    the exception, where the fallback is skipped). An empty cloud list is
    ambiguous (clear vs not reported) and yields no cloud row.
    """
    metar_cities = {c["metar"]: c["key"] for c in CITIES if c.get("metar")}
    if not metar_cities:
        return
    try:
        data = http_json(
            "https://aviationweather.gov/api/data/metar"
            f"?ids={','.join(metar_cities)}&format=json&hours=72"
        )
        # (city, hour, var) -> (seconds_from_hour, value); nearest report wins
        best = {}
        for m in data:
            city = metar_cities.get(m.get("icaoId"))
            t = m.get("reportTime")
            if not city or not t:
                continue
            dt = datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
            hour = (dt + timedelta(minutes=30)).replace(minute=0, second=0)
            dist = abs((dt - hour).total_seconds())
            hh = hour.strftime("%Y-%m-%dT%H:%MZ")
            temp, dewp = m.get("temp"), m.get("dewp")
            vals = {"t2m": temp, "td": dewp}
            if temp is not None and dewp is not None:
                vals["rh"] = round(_rh_from_t_td(float(temp), float(dewp)), 1)
            if isinstance(m.get("wdir"), (int, float)):
                vals["wdir"] = float(m["wdir"])
            if m.get("wspd") is not None:
                vals["ws"] = float(m["wspd"]) * 0.514444
            if m.get("wgst") is not None:
                vals["gust"] = float(m["wgst"]) * 0.514444
            if m.get("slp") is not None:
                vals["pmsl"] = float(m["slp"])
            elif m.get("altim") is not None and city != "denver":
                vals["pmsl"] = float(m["altim"])
            covers = [METAR_OCTAS.get(c.get("cover")) for c in (m.get("clouds") or [])]
            covers = [c for c in covers if c is not None]
            if covers:
                vals["cc"] = max(covers) * 12.5
            for var, v in vals.items():
                if v is None:
                    continue
                k = (city, hh, var)
                if k not in best or dist < best[k][0]:
                    best[k] = (dist, float(v))
        by_city = {}
        for (city, hh, var), (_, v) in best.items():
            by_city.setdefault(city, []).append((hh, var, v))
        for city, rows in by_city.items():
            store_observations(con, city, rows)
            log(con, run_time, "metar_obs", city, len(rows))
        missing = set(metar_cities.values()) - set(by_city)
        for city in missing:
            log(con, run_time, "metar_obs", city, 0, "no reports in window")
    except Exception as e:  # noqa: BLE001
        for city in metar_cities.values():
            log(con, run_time, "metar_obs", city, 0, str(e))
    con.commit()


SMHI_MAP = {"air_temperature": "t2m", "wind_speed": "ws", "wind_from_direction": "wdir",
            "wind_speed_of_gust": "gust", "relative_humidity": "rh",
            "air_pressure_at_mean_sea_level": "pmsl", "cloud_area_fraction": "cc"}


def collect_smhi(con, run_time):
    """Sweden's national forecast (SMHI open data, snow1g - the API that
    replaced pmp3g when it was shut down 2026-03-31). CC BY-class open data.
    Precipitation is an interval amount [intervalParametersStartTime, time];
    only 1 h intervals are stored, stamped hour-ending at `time`."""
    for city in CITIES:
        if city.get("country") != "se":
            continue
        try:
            data = http_json(
                "https://opendata-download-metfcst.smhi.se/api/category/snow1g/"
                f"version/1/geotype/point/lon/{round(city['lon'],2)}/lat/{round(city['lat'],2)}/data.json"
            )
            rows = []
            for step in data["timeSeries"]:
                t = step["time"][:16] + "Z"
                d = step.get("data", {})
                for k, var in SMHI_MAP.items():
                    if d.get(k) is not None:
                        rows.append(("smhi", city["key"], run_time, t, var, float(d[k])))
                start = step.get("intervalParametersStartTime")
                p1 = d.get("precipitation_amount_mean")
                if p1 is not None and start:
                    span = (datetime.strptime(step["time"][:16], "%Y-%m-%dT%H:%M")
                            - datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")).total_seconds()
                    if span == 3600:
                        rows.append(("smhi", city["key"], run_time, t, "rain1h", float(p1)))
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "smhi", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "smhi", city["key"], 0, str(e))
        time.sleep(0.3)
    con.commit()


BSKY_MAP = {"wind_speed": "ws", "wind_direction": "wdir", "wind_gust_speed": "gust",
            "relative_humidity": "rh", "cloud_cover": "cc"}


def collect_brightsky(con, run_time):
    """Germany's national forecast (DWD MOSMIX) via Bright Sky - the public
    no-key JSON front for DWD open data. units=si means KELVIN and PASCAL
    (learned the hard way: 297.85 K, 102010 Pa), converted at the door.
    Bright Sky's precipitation is the hour PRECEDING the timestamp - already
    our hour-ending convention."""
    end = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for city in CITIES:
        if city.get("country") != "de":
            continue
        try:
            data = http_json(
                f"https://api.brightsky.dev/weather?lat={city['lat']}&lon={city['lon']}"
                f"&date={today}&last_date={end}&units=si"
            )
            rows = []
            for h in data.get("weather", []):
                ts = h["timestamp"]
                t = (datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                     - timedelta(hours=int(ts[19:22] or 0))).strftime("%Y-%m-%dT%H:%MZ")                     if len(ts) > 19 and ts[19] in "+-" else ts[:16] + "Z"
                d = dict(h)
                if d.get("temperature") is not None:
                    rows.append(("dwd", city["key"], run_time, t, "t2m", d["temperature"] - 273.15))
                if d.get("dew_point") is not None:
                    rows.append(("dwd", city["key"], run_time, t, "td", d["dew_point"] - 273.15))
                if d.get("pressure_msl") is not None:
                    rows.append(("dwd", city["key"], run_time, t, "pmsl", d["pressure_msl"] / 100.0))
                if d.get("precipitation") is not None:
                    rows.append(("dwd", city["key"], run_time, t, "rain1h", float(d["precipitation"])))
                for k, var in BSKY_MAP.items():
                    if d.get(k) is not None:
                        rows.append(("dwd", city["key"], run_time, t, var, float(d[k])))
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "dwd", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "dwd", city["key"], 0, str(e))
        time.sleep(0.3)
    con.commit()


def collect_nws(con, run_time):
    """The US national forecast (api.weather.gov, public domain, no key).
    Two hops: points/{lat,lon} names the gridpoint, whose hourly forecast is
    then fetched with units=si. Wind arrives as the string "12 km/h" and the
    direction as a compass point - both parsed. ~6.5 days of hours."""
    for city in CITIES:
        if city.get("country") != "us":
            continue
        try:
            pt = http_json(f"https://api.weather.gov/points/{city['lat']},{city['lon']}")
            url = pt["properties"]["forecastHourly"] + "?units=si"
            data = http_json(url)
            rows = []
            for per in data["properties"]["periods"]:
                ts = per["startTime"]
                # "2026-08-26T12:00:00-05:00": UTC = local minus the signed offset
                sign = -1 if ts[19] == "-" else 1
                off = sign * (int(ts[20:22]) * 60 + int(ts[23:25]))
                t = (datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                     - timedelta(minutes=off)).strftime("%Y-%m-%dT%H:%MZ")
                if per.get("temperature") is not None and per.get("temperatureUnit") == "C":
                    rows.append(("nws", city["key"], run_time, t, "t2m", float(per["temperature"])))
                wsp = per.get("windSpeed") or ""
                if wsp.endswith("km/h"):
                    rows.append(("nws", city["key"], run_time, t, "ws", float(wsp.split()[0]) / 3.6))
                wd = COMPASS.get(per.get("windDirection") or "")
                if wd is not None:
                    rows.append(("nws", city["key"], run_time, t, "wdir", wd))
                rh = (per.get("relativeHumidity") or {}).get("value")
                if rh is not None:
                    rows.append(("nws", city["key"], run_time, t, "rh", float(rh)))
                dp = (per.get("dewpoint") or {}).get("value")
                if dp is not None:
                    rows.append(("nws", city["key"], run_time, t, "td", float(dp)))
            if not rows:
                raise RuntimeError("0 rows parsed")
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "nws", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "nws", city["key"], 0, str(e))
        time.sleep(0.5)
    con.commit()


def collect_obs(con, run_time):
    # 166h window (just under FMI's 168h interval cap): a session outage of up to
    # ~6 days backfills itself on the next run instead of leaving a permanent hole.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=166)).strftime("%Y-%m-%dT%H:00:00Z")
    end = now.strftime("%Y-%m-%dT%H:00:00Z")
    for city in CITIES:
        if city.get("country") != "fi":
            continue
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
    collect_smhi(con, run_time)
    collect_brightsky(con, run_time)
    collect_nws(con, run_time)
    collect_obs(con, run_time)
    collect_metar_obs(con, run_time)
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
