"""Score accrued prospective forecasts (collect.py snapshots) against observations.

Two boards:
  1. Hourly t2m MAE by lead day - fmi_edited vs each Open-Meteo model.
  2. Daily tmin/tmax MAE by lead day - adds Foreca (its feed is daily-only).
     Daily values for hourly sources & obs are min/max over Europe/Helsinki
     local days (>=20 obs hours required); Foreca's own daily values are trusted as-is.

Usage: python3 score.py
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from common import get_db, DATA_DIR

HKI = ZoneInfo("Europe/Helsinki")


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _local_date(s: str) -> str:
    return _utc(s).astimezone(HKI).strftime("%Y-%m-%d")


def _stat(cells, key, err):
    s = cells.setdefault(key, {"n": 0, "sae": 0.0, "se": 0.0})
    s["n"] += 1
    s["sae"] += abs(err)
    s["se"] += err


def _finish(cells):
    return {
        k: {"n": s["n"], "mae": round(s["sae"] / s["n"], 3), "bias": round(s["se"] / s["n"], 3)}
        for k, s in cells.items()
    }


def hourly_board(con) -> dict:
    obs = {
        (c, t): v
        for c, t, v in con.execute("SELECT city, time, value FROM observations WHERE var='t2m'")
    }
    cells: dict = {}
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='t2m'"
    for source, city, run_time, target_time, value in con.execute(q):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        lead_d = int(lead_h // 24)
        _stat(cells, (source, lead_d), value - truth)
    out: dict = defaultdict(dict)
    for (source, lead_d), s in _finish(cells).items():
        out[source][str(lead_d)] = s
    return dict(out)


def daily_series(con) -> tuple[dict, dict]:
    """Returns (obs_daily, fc_daily).
    obs_daily[(city, date)] = {tmin, tmax}
    fc_daily[(source, city, run_time, date)] = {tmin, tmax}
    """
    by_day: dict = defaultdict(list)
    for c, t, v in con.execute("SELECT city, time, value FROM observations WHERE var='t2m'"):
        by_day[(c, _local_date(t))].append(v)
    obs_daily = {
        k: {"tmin": min(vs), "tmax": max(vs)} for k, vs in by_day.items() if len(vs) >= 20
    }

    fc_daily: dict = {}
    # Foreca's native daily feed, kept as its own source ("foreca_daily") since
    # hourly-derived "foreca" daily values exist too and use the same methodology
    # as every other source.
    q = "SELECT city, run_time, target_time, var, value FROM forecasts WHERE source='foreca' AND var IN ('tmin','tmax')"
    for city, run_time, date, var, value in con.execute(q):
        fc_daily.setdefault(("foreca_daily", city, run_time, date), {})[var] = value
    # Hourly sources: derive local-day min/max, only for fully-covered days (>=23 hours)
    hr: dict = defaultdict(list)
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='t2m'"
    for source, city, run_time, target_time, value in con.execute(q):
        hr[(source, city, run_time, _local_date(target_time))].append(value)
    for k, vs in hr.items():
        if len(vs) >= 23:
            fc_daily[k] = {"tmin": min(vs), "tmax": max(vs)}
    return obs_daily, fc_daily


def daily_board(con) -> dict:
    obs_daily, fc_daily = daily_series(con)
    cells: dict = {}
    for (source, city, run_time, date), fc in fc_daily.items():
        truth = obs_daily.get((city, date))
        if truth is None:
            continue
        run_date = _utc(run_time).astimezone(HKI).strftime("%Y-%m-%d")
        lead_d = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(run_date, "%Y-%m-%d")).days
        if lead_d < 0:
            continue
        for var in ("tmin", "tmax"):
            if var in fc:
                _stat(cells, (source, var, lead_d), fc[var] - truth[var])
    out: dict = defaultdict(dict)
    for (source, var, lead_d), s in _finish(cells).items():
        out[source][f"{var}_d{lead_d}"] = s
    return dict(out)


def print_boards(hourly: dict, daily: dict):
    def table(title, results, key_fmt):
        if not results:
            print(f"\n{title}: no scorable data yet")
            return
        keys = sorted({k for m in results.values() for k in m}, key=key_fmt)
        models = sorted(results, key=lambda m: results[m].get(keys[0], {}).get("mae", 9e9))
        print(f"\n{title}  (MAE degC, lower is better; n in parens)")
        print(f"{'':>12}" + "".join(f"{k:>16}" for k in keys))
        for m in models:
            row = f"{m[:12]:>12}"
            for k in keys:
                s = results[m].get(k)
                row += f"  {s['mae']:.2f} ({s['n']:>4})" if s else f"{'-':>16}"
            print(row)

    table("Hourly t2m by lead day", hourly, key_fmt=lambda k: int(k))
    table("Daily tmin/tmax by lead day", daily, key_fmt=lambda k: (k.split("_d")[0], int(k.split("_d")[1])))


def main():
    con = get_db()
    n_runs = con.execute("SELECT count(DISTINCT run_time) FROM forecasts").fetchone()[0]
    print(f"Scoring prospective data: {n_runs} collection runs in DB")
    hourly = hourly_board(con)
    daily = daily_board(con)
    print_boards(hourly, daily)
    (DATA_DIR / "prospective_results.json").write_text(
        json.dumps({"hourly_t2m": hourly, "daily": daily}, indent=2)
    )
    print(f"\nSaved {DATA_DIR / 'prospective_results.json'}")


if __name__ == "__main__":
    main()
