"""Prospective collector: snapshot everyone's live forecast + recent observations.

Run every ~6-12h. Each run stores, per city:
  - Foreca daily forecast (tmin/tmax/rain, ~13 days)    [source=foreca, daily vars]
  - Foreca hourly t2m/ws/rain1h, ~11 days (scraped from the server-rendered
    hour_data blob on foreca.fi /details pages)          [source=foreca, hourly vars]
  - FMI edited (human-curated) hourly t2m/ws/rain1h, ~10 days [source=fmi_edited]
  - Open-Meteo hourly t2m/ws/rain1h per model, 16 days  [source=<model id>]
  - FMI station observations for the last 48h (verification truth)

run_time = collection hour (UTC). Lead time is computed at scoring from run_time,
which slightly flatters no one in particular: every source is snapshotted at the
same wall-clock moment, exactly like a user opening two weather apps side by side.

Usage: python3 collect.py
"""
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import (
    CITIES, OM_MODELS, get_db, http_get, http_json, fmi_simple,
    fetch_obs, store_observations,
)

HKI = ZoneInfo("Europe/Helsinki")
# FMI edited-forecast parameter -> canonical var
FMI_FC_PARAMS = {"temperature": "t2m", "windspeedms": "ws", "precipitation1h": "rain1h"}
# Open-Meteo hourly variable -> canonical var
OM_VARS = {"temperature_2m": "t2m", "wind_speed_10m": "ws", "precipitation": "rain1h"}


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
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                rows,
            )
            log(con, run_time, "foreca", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001 - one source failing must not kill the run
            log(con, run_time, "foreca", city["key"], 0, str(e))
        time.sleep(0.4)


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
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                [("fmi_edited", city["key"], run_time, t, FMI_FC_PARAMS[p], v)
                 for (t, p, v) in rows if p in FMI_FC_PARAMS],
            )
            log(con, run_time, "fmi_edited", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "fmi_edited", city["key"], 0, str(e))
        time.sleep(0.4)


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
                    rows = [
                        (model, city["key"], run_time, t, var, v)
                        for t, v in zip(times, values) if v is not None
                    ]
                    con.executemany(
                        "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                        rows,
                    )
                    n += len(rows)
            log(con, run_time, "open_meteo", city["key"], n)
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "open_meteo", city["key"], 0, str(e))
        time.sleep(0.4)


def collect_obs(con, run_time):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:00:00Z")
    end = now.strftime("%Y-%m-%dT%H:00:00Z")
    for city in CITIES:
        try:
            rows = fetch_obs(city, start, end)
            store_observations(con, city["key"], rows)
            log(con, run_time, "obs", city["key"], len(rows))
        except Exception as e:  # noqa: BLE001
            log(con, run_time, "obs", city["key"], 0, str(e))
        time.sleep(0.4)


def main():
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00Z")
    con = get_db()
    print(f"collect run {run_time}")
    collect_foreca(con, run_time)
    collect_foreca_hourly(con, run_time)
    collect_fmi_edited(con, run_time)
    collect_open_meteo(con, run_time)
    collect_obs(con, run_time)
    con.commit()
    errs = con.execute(
        "SELECT count(*) FROM collect_log WHERE run_time=? AND error IS NOT NULL", (run_time,)
    ).fetchone()[0]
    total = con.execute(
        "SELECT coalesce(sum(rows),0) FROM collect_log WHERE run_time=?", (run_time,)
    ).fetchone()[0]
    print(f"done: {total} rows stored, {errs} errors")
    con.close()


if __name__ == "__main__":
    main()
