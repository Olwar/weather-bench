"""Retrospective benchmark: AI models vs physics models over the recent past.

Uses Open-Meteo's previous-runs archive (what each model actually forecast N days
ahead, as issued at the time) and verifies against FMI station observations.
This answers "how good are the new models in Finland" today, without waiting.

Competitors (Foreca / FMI edited forecast) can't be tested retrospectively -
no public archive of their past forecasts - that's what collect.py accrues.

Usage: python3 retro.py [days]   (default 90, max ~92)
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from common import CITIES, OM_MODELS, get_db, http_json, fetch_obs, store_observations, DATA_DIR

MAX_LEAD = 7


def fetch_retro_forecasts(con, days: int):
    # Wipe first: a shorter rerun must not silently inherit rows from an older,
    # longer fetch - the scored table must be exactly one fetch's worth of data.
    con.execute("DELETE FROM retro_forecasts")
    con.commit()
    hourly_vars = ",".join(f"temperature_2m_previous_day{d}" for d in range(1, MAX_LEAD + 1))
    models = ",".join(OM_MODELS)
    for city in CITIES:
        url = (
            "https://previous-runs-api.open-meteo.com/v1/forecast"
            f"?latitude={city['lat']}&longitude={city['lon']}"
            f"&hourly={hourly_vars}&models={models}&past_days={days}&forecast_days=1"
        )
        try:
            data = http_json(url)
            blocks = data if isinstance(data, list) else [data]
            n_rows = 0
            for block in blocks:
                hourly = block.get("hourly", {})
                times = [t + "Z" for t in hourly.get("time", [])]
                for key, values in hourly.items():
                    if key == "time":
                        continue
                    # key: temperature_2m_previous_day3_ecmwf_ifs025 (or no model suffix if single)
                    model = next((m for m in OM_MODELS if key.endswith("_" + m)), None)
                    stem = key[: -(len(model) + 1)] if model else key
                    if model is None and len(OM_MODELS) == 1:
                        model = OM_MODELS[0]
                    if not stem.startswith("temperature_2m_previous_day") or model is None:
                        continue
                    lead = int(stem.rsplit("day", 1)[1])
                    rows = [
                        (model, city["key"], lead, t, v)
                        for t, v in zip(times, values)
                        if v is not None
                    ]
                    con.executemany(
                        "INSERT OR REPLACE INTO retro_forecasts(model, city, lead_days, target_time, value)"
                        " VALUES(?,?,?,?,?)",
                        rows,
                    )
                    n_rows += len(rows)
            con.commit()
            print(f"  forecasts {city['key']}: {n_rows} rows" + (" (SUSPICIOUS: 0)" if n_rows == 0 else ""))
        except Exception as e:  # noqa: BLE001 - keep other cities' fetches alive
            print(f"  forecasts {city['key']}: FAILED {e}")
        time.sleep(0.5)


def fetch_retro_obs(con, days: int):
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days + 1)
    for city in CITIES:
        t = start
        n = 0
        while t < end:
            t2 = min(t + timedelta(days=6), end)
            rows = fetch_obs(city, t.strftime("%Y-%m-%dT%H:%M:%SZ"), t2.strftime("%Y-%m-%dT%H:%M:%SZ"))
            store_observations(con, city["key"], rows)
            n += len(rows)
            t = t2
            time.sleep(0.3)
        con.commit()
        print(f"  obs {city['key']}: {n} rows")


GLOBAL_MODELS = ["ecmwf_ifs025", "ecmwf_aifs025_single", "best_match"]


def score(con) -> dict:
    """MAE / RMSE / bias per model per lead day, hourly t2m, all cities pooled.

    Matched comparison: a (city, lead, target) sample only counts if EVERY model
    in GLOBAL_MODELS has a value there - averaging models over different sample
    sets is not a fair comparison. Models outside GLOBAL_MODELS (metno_nordic,
    day-1 only) are scored on their own samples and marked unmatched.
    """
    q = """
      SELECT f.model, f.city, f.lead_days, f.target_time, f.value - o.value AS err
      FROM retro_forecasts f
      JOIN observations o ON o.city = f.city AND o.time = f.target_time AND o.var = 't2m'
    """
    by_sample: dict = {}
    for model, city, lead, target, err in con.execute(q):
        by_sample.setdefault((city, lead, target), {})[model] = err
    stats: dict = {}
    for (_city, lead, _target), errs in by_sample.items():
        matched = all(m in errs for m in GLOBAL_MODELS)
        for model, err in errs.items():
            if model in GLOBAL_MODELS and not matched:
                continue
            s = stats.setdefault(model, {}).setdefault(lead, {"n": 0, "sae": 0.0, "sse": 0.0, "se": 0.0})
            s["n"] += 1
            s["sae"] += abs(err)
            s["sse"] += err * err
            s["se"] += err
    out = {}
    for model, leads in stats.items():
        out[model] = {
            str(lead): {
                "n": s["n"],
                "mae": round(s["sae"] / s["n"], 3),
                "rmse": round((s["sse"] / s["n"]) ** 0.5, 3),
                "bias": round(s["se"] / s["n"], 3),
                "matched": model in GLOBAL_MODELS,
            }
            for lead, s in sorted(leads.items())
        }
    return out


def print_table(results: dict):
    models = sorted(results, key=lambda m: results[m].get("1", {}).get("mae", 9e9))
    leads = sorted({int(l) for m in results.values() for l in m})
    print(f"\nHourly 2m-temperature MAE (degC) vs FMI stations, {len(CITIES)} cities pooled")
    print(f"(matched samples across {', '.join(GLOBAL_MODELS)}; metno_nordic scored on its own samples)")
    header = "lead(d) " + "".join(f"{m[:22]:>24}" for m in models)
    print(header)
    for lead in leads:
        row = f"{lead:>6}  "
        for m in models:
            s = results[m].get(str(lead))
            row += f"{s['mae']:>22.2f}  " if s else f"{'-':>22}  "
        print(row)
    print(f"\n(lower is better; n per cell in retro_results.json)")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    con = get_db()
    print(f"Retrospective benchmark, past {days} days, {len(CITIES)} cities")
    print("Fetching archived forecasts (previous-runs API)...")
    fetch_retro_forecasts(con, days)
    print("Fetching FMI observations...")
    fetch_retro_obs(con, days)
    results = score(con)
    print_table(results)
    out_path = DATA_DIR / "retro_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
