"""Prospective collector: snapshot everyone's live forecast + recent observations.

Run every ~6-12h. Each run stores, per city:
  - Foreca daily forecast (tmin/tmax/rain, ~13 days)   [source=foreca]
  - FMI edited (human-curated) hourly t2m, ~10 days     [source=fmi_edited]
  - Open-Meteo hourly t2m for each model in OM_MODELS   [source=<model id>]
  - FMI station observations for the last 48h (verification truth)

run_time = collection hour (UTC). Lead time is computed at scoring from run_time,
which slightly flatters no one in particular: every source is snapshotted at the
same wall-clock moment, exactly like a user opening two weather apps side by side.

Usage: python3 collect.py
"""
import time
from datetime import datetime, timedelta, timezone

from common import (
    CITIES, OM_MODELS, get_db, http_json, fmi_simple,
    fetch_obs_t2m, store_observations,
)


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


def collect_fmi_edited(con, run_time):
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:00:00Z")
    end = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:00:00Z")
    for city in CITIES:
        try:
            rows = fmi_simple(
                "fmi::forecast::edited::weather::scandinavia::point::simple",
                place=city["fmi_place"], parameters="temperature", timestep=60,
                starttime=start, endtime=end,
            )
            con.executemany(
                "INSERT OR IGNORE INTO forecasts(source,city,run_time,target_time,var,value) VALUES(?,?,?,?,?,?)",
                [("fmi_edited", city["key"], run_time, t, "t2m", v) for (t, _p, v) in rows],
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
                f"&hourly=temperature_2m&models={models}&forecast_days=10"
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
                    if model is None and len(OM_MODELS) == 1:
                        model = OM_MODELS[0]
                    if model is None:
                        continue
                    rows = [
                        (model, city["key"], run_time, t, "t2m", v)
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
            rows = fetch_obs_t2m(city, start, end)
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
