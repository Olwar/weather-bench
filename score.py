"""Score accrued prospective forecasts (collect.py snapshots) against observations.

Boards:
  1. Hourly t2m MAE by lead day - all hourly sources incl. Foreca.
  2. Hourly wind speed MAE by lead day.
  3. Rain occurrence skill by lead day (threshold 0.1 mm/h): POD/FAR/CSI.
  4. Daily tmin/tmax MAE by lead day (foreca_daily = Foreca's native daily feed).
  5. Daily rain total MAE by lead day.
  6. Pairwise significance: candidate vs competitor MAE difference with a
     date-block bootstrap 95% CI and win rate. This is the "can I advertise it"
     table: a claim is supportable only when the CI excludes zero.

Daily values for hourly sources & obs are min/max/sum over Europe/Helsinki local
days (>=23 forecast hours, >=20 obs hours required).

Usage: python3 score.py
"""
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from common import get_db, DATA_DIR

HKI = ZoneInfo("Europe/Helsinki")
RAIN_THR = 0.1     # mm/h for occurrence skill
N_BOOT = 2000
CANDIDATES = ["ecmwf_aifs025_single", "best_match"]
COMPETITORS = ["foreca", "fmi_edited"]


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


def _load_obs(con, var):
    return {
        (c, t): v
        for c, t, v in con.execute("SELECT city, time, value FROM observations WHERE var=?", (var,))
    }


def hourly_board(con, var: str) -> dict:
    obs = _load_obs(con, var)
    cells: dict = {}
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var=?"
    for source, city, run_time, target_time, value in con.execute(q, (var,)):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        _stat(cells, (source, int(lead_h // 24)), value - truth)
    out: dict = defaultdict(dict)
    for (source, lead_d), s in _finish(cells).items():
        out[source][str(lead_d)] = s
    return dict(out)


def rain_occurrence_board(con) -> dict:
    """POD (hit rate), FAR, CSI for rain/no-rain at RAIN_THR, by lead day."""
    obs = _load_obs(con, "rain1h")
    cells: dict = defaultdict(lambda: {"hit": 0, "miss": 0, "fa": 0, "cn": 0})
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='rain1h'"
    for source, city, run_time, target_time, value in con.execute(q):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        key = (source, int(lead_h // 24))
        fc_rain, ob_rain = value >= RAIN_THR, truth >= RAIN_THR
        if fc_rain and ob_rain:
            cells[key]["hit"] += 1
        elif not fc_rain and ob_rain:
            cells[key]["miss"] += 1
        elif fc_rain and not ob_rain:
            cells[key]["fa"] += 1
        else:
            cells[key]["cn"] += 1
    out: dict = defaultdict(dict)
    for (source, lead_d), c in cells.items():
        hits, miss, fa = c["hit"], c["miss"], c["fa"]
        n = hits + miss + fa + c["cn"]
        out[source][str(lead_d)] = {
            "n": n,
            "pod": round(hits / (hits + miss), 3) if hits + miss else None,
            "far": round(fa / (hits + fa), 3) if hits + fa else None,
            "csi": round(hits / (hits + miss + fa), 3) if hits + miss + fa else None,
        }
    return dict(out)


def daily_series(con):
    """obs_daily[(city, date)] = {tmin,tmax,rain}; fc_daily[(source,city,run_time,date)] = same."""
    t_by_day: dict = defaultdict(list)
    r_by_day: dict = defaultdict(list)
    for c, t, var, v in con.execute("SELECT city, time, var, value FROM observations WHERE var IN ('t2m','rain1h')"):
        (t_by_day if var == "t2m" else r_by_day)[(c, _local_date(t))].append(v)
    obs_daily: dict = {}
    for k, vs in t_by_day.items():
        if len(vs) >= 20:
            obs_daily[k] = {"tmin": min(vs), "tmax": max(vs)}
    for k, vs in r_by_day.items():
        if len(vs) >= 20 and k in obs_daily:
            obs_daily[k]["rain"] = sum(vs)

    fc_daily: dict = {}
    q = "SELECT city, run_time, target_time, var, value FROM forecasts WHERE source='foreca' AND var IN ('tmin','tmax','rain')"
    for city, run_time, date, var, value in con.execute(q):
        fc_daily.setdefault(("foreca_daily", city, run_time, date), {})[var] = value
    hr_t: dict = defaultdict(list)
    hr_r: dict = defaultdict(list)
    q = "SELECT source, city, run_time, target_time, var, value FROM forecasts WHERE var IN ('t2m','rain1h')"
    for source, city, run_time, target_time, var, value in con.execute(q):
        # Feeds include already-elapsed hours of the snapshot day (analysis, not
        # forecast) - grading on them would be hindsight. Mirror hourly_board's filter.
        if _utc(target_time) < _utc(run_time):
            continue
        k = (source, city, run_time, _local_date(target_time))
        (hr_t if var == "t2m" else hr_r)[k].append(value)
    for k, vs in hr_t.items():
        if len(vs) >= 23:
            fc_daily[k] = {"tmin": min(vs), "tmax": max(vs)}
    for k, vs in hr_r.items():
        if len(vs) >= 23 and k in fc_daily:
            fc_daily[k]["rain"] = sum(vs)
    return obs_daily, fc_daily


def daily_board(con, obs_daily, fc_daily) -> dict:
    cells: dict = {}
    for (source, city, run_time, date), fc in fc_daily.items():
        truth = obs_daily.get((city, date))
        if truth is None:
            continue
        run_date = _utc(run_time).astimezone(HKI).strftime("%Y-%m-%d")
        lead_d = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(run_date, "%Y-%m-%d")).days
        # d0 is excluded entirely: part of the day has already happened at snapshot
        # time, so "today" scores would mix forecast with hindsight (and Foreca's
        # native daily today-entry cannot be filtered hour-wise at all).
        if lead_d < 1:
            continue
        for var in ("tmin", "tmax", "rain"):
            if var in fc and var in truth:
                _stat(cells, (source, var, lead_d), fc[var] - truth[var])
    out: dict = defaultdict(dict)
    for (source, var, lead_d), s in _finish(cells).items():
        out[source][f"{var}_d{lead_d}"] = s
    return dict(out)


def _bootstrap_diff(samples_by_date: dict, n_boot: int = N_BOOT):
    """samples_by_date: date -> [(errA, errB), ...]. Bootstrap dates (blocks).
    Returns (diff, ci_lo, ci_hi) of MAE_A - MAE_B."""
    dates = list(samples_by_date)
    rng = random.Random(42)
    diffs = []
    for _ in range(n_boot):
        sa = sse = 0.0
        n = 0
        for d in (rng.choice(dates) for _ in dates):
            for ea, eb in samples_by_date[d]:
                sa += abs(ea)
                sse += abs(eb)
                n += 1
        if n:
            diffs.append((sa - sse) / n)
    diffs.sort()
    point = sum(diffs) / len(diffs)
    return point, diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]


def pairwise(con, cand: str, comp: str, var: str = "t2m") -> dict:
    """Matched-sample comparison: same city, same snapshot, same target hour."""
    obs = _load_obs(con, var)
    errs: dict = defaultdict(dict)
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var=? AND source IN (?,?)"
    for source, city, run_time, target_time, value in con.execute(q, (var, cand, comp)):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        errs[(city, run_time, target_time)][source] = value - truth
    by_lead: dict = defaultdict(lambda: defaultdict(list))
    for (city, run_time, target_time), pair in errs.items():
        if cand in pair and comp in pair:
            lead_d = int((_utc(target_time) - _utc(run_time)).total_seconds() / 3600 // 24)
            by_lead[lead_d][_local_date(target_time)].append((pair[cand], pair[comp]))
    out = {}
    for lead_d, by_date in sorted(by_lead.items()):
        pairs = [p for ps in by_date.values() for p in ps]
        if len(pairs) < 10 or len(by_date) < 3:
            continue
        diff, lo, hi = _bootstrap_diff(by_date)
        decided = [(ea, eb) for ea, eb in pairs if abs(ea) != abs(eb)]
        win = sum(1 for ea, eb in decided if abs(ea) < abs(eb)) / len(decided) if decided else None
        out[str(lead_d)] = {
            "n": len(pairs),
            "days": len(by_date),
            "mae_cand": round(sum(abs(a) for a, _ in pairs) / len(pairs), 3),
            "mae_comp": round(sum(abs(b) for _, b in pairs) / len(pairs), 3),
            "diff": round(diff, 3),
            "ci95": [round(lo, 3), round(hi, 3)],
            "win_rate": round(win, 3) if win is not None else None,
            "significant": bool(hi < 0 or lo > 0),
        }
    return out


def print_board(title, results, unit="degC MAE"):
    if not results:
        print(f"\n{title}: no scorable data yet")
        return
    keys = sorted({k for m in results.values() for k in m}, key=lambda k: (k.split("_d")[0], int(k.split("_d")[-1])) if "_d" in k else int(k))
    first = keys[0]
    metric = "mae" if "mae" in next(iter(results.values())).get(first, {"mae": 0}) else "csi"
    models = sorted(results, key=lambda m: results[m].get(first, {}).get(metric, 9e9) or 9e9)
    print(f"\n{title}  ({unit}; n in parens)")
    print(f"{'':>14}" + "".join(f"{k:>15}" for k in keys))
    for m in models:
        row = f"{m[:14]:>14}"
        for k in keys:
            s = results[m].get(k)
            if not s or s.get(metric) is None:
                row += f"{'-':>15}"
            else:
                row += f"  {s[metric]:>5.2f} ({s['n']:>5})"
        print(row)


def print_pairwise(all_pairs: dict):
    print("\n=== Pairwise significance (candidate vs competitor, hourly t2m MAE) ===")
    print("negative diff = candidate better; claim supportable only when CI excludes 0")
    for (cand, comp), leads in all_pairs.items():
        print(f"\n{cand} vs {comp}:")
        if not leads:
            print("  not enough matched data yet")
            continue
        print(f"{'lead':>5}{'n':>7}{'days':>6}{'cand':>7}{'comp':>7}{'diff':>7}{'CI95':>18}{'win%':>7}  sig")
        for lead, s in leads.items():
            ci = f"[{s['ci95'][0]:+.2f},{s['ci95'][1]:+.2f}]"
            win = f"{100*s['win_rate']:.0f}" if s["win_rate"] is not None else "-"
            mark = "YES" if s["significant"] else "no"
            print(f"{lead:>5}{s['n']:>7}{s['days']:>6}{s['mae_cand']:>7.2f}{s['mae_comp']:>7.2f}{s['diff']:>+7.2f}{ci:>18}{win:>7}  {mark}")


def main():
    con = get_db()
    n_runs = con.execute("SELECT count(DISTINCT run_time) FROM forecasts").fetchone()[0]
    print(f"Scoring prospective data: {n_runs} collection runs in DB")

    t2m = hourly_board(con, "t2m")
    ws = hourly_board(con, "ws")
    rain_occ = rain_occurrence_board(con)
    obs_daily, fc_daily = daily_series(con)
    daily = daily_board(con, obs_daily, fc_daily)
    pairs = {
        (cand, comp): pairwise(con, cand, comp)
        for cand in CANDIDATES for comp in COMPETITORS
    }

    print_board("Hourly t2m by lead day", t2m)
    print_board("Hourly wind speed by lead day", ws, unit="m/s MAE")
    print_board("Rain occurrence (>=0.1mm/h) by lead day", rain_occ, unit="CSI, higher better")
    print_board("Daily tmin/tmax/rain by lead day", daily, unit="degC / mm MAE")
    print_pairwise(pairs)

    (DATA_DIR / "prospective_results.json").write_text(json.dumps({
        "hourly_t2m": t2m, "hourly_ws": ws, "rain_occurrence": rain_occ, "daily": daily,
        "pairwise_t2m": {f"{a}__vs__{b}": v for (a, b), v in pairs.items()},
    }, indent=2))
    print(f"\nSaved {DATA_DIR / 'prospective_results.json'}")


if __name__ == "__main__":
    main()
