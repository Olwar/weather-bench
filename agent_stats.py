"""Nightly precompute for the agent-facing (WebMCP) endpoints.

Turns the verified record into three compact, queryable artifacts:

1. t2m error quantiles conditioned on (lead day, member spread) - the
   calibration table that converts "blend 18.2, members 17-21, day 4" into an
   honest probability. Conditioning on spread is the point: the models'
   disagreement is informative about the error, and 30+ days of verification
   tell us exactly how informative.
2. Rain occurrence calibration by (lead, forecast intensity bucket): the
   observed frequency of measurable rain (>=0.1 mm) for each bucket - a
   frequency table, no parametric assumptions.
3. Forecast-churn norms: how much a single model's forecast for a given target
   day typically wobbles between successive runs, by lead. The live stability
   endpoint compares today's wobble to these percentiles.

Runs in the nightly chain AFTER score.py and before the blend wipe (it does
not need blend rows - the blend is recomputed as the member mean here).
Everything is a single pass per (city, source) using the primary-key index;
no full-table scans.
"""
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

from common import get_db, DATA_DIR, CITIES
from blend import OPEN_MEMBERS

SPREAD_EDGES = (0.6, 1.5)      # degC member stddev -> low / mid / high agreement
RAIN_BUCKETS = ((0.0, 0.05), (0.05, 0.3), (0.3, 1.0), (1.0, 99.0))
MAX_LEAD = 10
RESERVOIR = 20000              # per-bucket error sample cap; quantiles from sample
QUANTS = (5, 10, 25, 50, 75, 90, 95)


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ")


def _spread_bucket(sd):
    return 0 if sd < SPREAD_EDGES[0] else (1 if sd < SPREAD_EDGES[1] else 2)


def main():
    con = get_db()
    rng = random.Random(42)

    t2m_res = defaultdict(list)          # (lead, spread_bucket) -> error sample
    t2m_seen = defaultdict(int)
    rain_cnt = defaultdict(lambda: [0, 0])   # (lead, bucket) -> [n, n_wet]
    churn_by_lead = defaultdict(list)        # lead_bucket -> per-(city,day) stddevs

    for city in CITIES:
        key = city["key"]
        obs_t = {t: v for t, v in con.execute(
            "SELECT time, value FROM observations WHERE city=? AND var='t2m'", (key,))}
        obs_r = {t: v for t, v in con.execute(
            "SELECT time, value FROM observations WHERE city=? AND var='rain1h'", (key,))}

        # member values grouped per (run, target); per-source queries ride the PK
        acc_t = defaultdict(list)
        acc_r = defaultdict(list)
        for src in OPEN_MEMBERS:
            for rt, tt, v in con.execute(
                "SELECT run_time, target_time, value FROM forecasts"
                " WHERE source=? AND city=? AND var='t2m'", (src, key)):
                if tt in obs_t and tt >= rt:
                    acc_t[(rt, tt)].append(v)
            for rt, tt, v in con.execute(
                "SELECT run_time, target_time, value FROM forecasts"
                " WHERE source=? AND city=? AND var='rain1h'", (src, key)):
                if tt in obs_r and tt >= rt:
                    acc_r[(rt, tt)].append(v)

        for (rt, tt), vals in acc_t.items():
            if len(vals) < 2:
                continue
            lead = int((_utc(tt) - _utc(rt)).total_seconds() // 86400)
            if lead >= MAX_LEAD:
                continue
            err = sum(vals) / len(vals) - obs_t[tt]
            b = (lead, _spread_bucket(statistics.pstdev(vals)))
            t2m_seen[b] += 1
            r = t2m_res[b]
            if len(r) < RESERVOIR:
                r.append(err)
            else:                        # reservoir sampling keeps it unbiased
                j = rng.randrange(t2m_seen[b])
                if j < RESERVOIR:
                    r[j] = err

        for (rt, tt), vals in acc_r.items():
            if len(vals) < 2:
                continue
            lead = int((_utc(tt) - _utc(rt)).total_seconds() // 86400)
            if lead >= MAX_LEAD:
                continue
            f = sum(vals) / len(vals)
            for bi, (lo, hi) in enumerate(RAIN_BUCKETS):
                if lo <= f < hi:
                    c = rain_cnt[(lead, bi)]
                    c[0] += 1
                    c[1] += 1 if obs_r[tt] >= 0.1 else 0
                    break

        # churn: one deterministic model's per-run daily means for each target day
        daily = defaultdict(dict)        # target_date -> run_time -> [temps]
        for rt, tt, v in con.execute(
            "SELECT run_time, target_time, value FROM forecasts"
            " WHERE source='ecmwf_aifs025_single' AND city=? AND var='t2m'", (key,)):
            if tt >= rt:
                daily[tt[:10]].setdefault(rt, []).append(v)
        for day, runs in daily.items():
            per_lead = defaultdict(list)
            for rt, temps in runs.items():
                if len(temps) >= 12:
                    lead = int((_utc(day + "T12:00Z") - _utc(rt)).total_seconds() // 86400)
                    if 0 <= lead < MAX_LEAD:
                        per_lead[lead].append(sum(temps) / len(temps))
            for lead, means in per_lead.items():
                if len(means) >= 3:
                    churn_by_lead[lead].append(statistics.pstdev(means))

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "spread_edges": SPREAD_EDGES,
        "t2m_error_quantiles": {
            f"{lead},{sb}": {
                "n": t2m_seen[(lead, sb)],
                **{f"q{q}": round(statistics.quantiles(sorted(v), n=100)[q - 1], 2)
                   for q in QUANTS},
            }
            for (lead, sb), v in sorted(t2m_res.items()) if len(v) >= 200
        },
        "rain_calibration": {
            f"{lead},{bi}": {"n": c[0], "p_wet": round(c[1] / c[0], 3)}
            for (lead, bi), c in sorted(rain_cnt.items()) if c[0] >= 200
        },
        "rain_buckets": RAIN_BUCKETS,
        "churn_norms": {
            str(lead): {
                "n": len(v),
                "p50": round(statistics.median(v), 2),
                "p80": round(sorted(v)[int(0.8 * len(v))], 2),
                "p95": round(sorted(v)[int(0.95 * len(v))], 2),
            }
            for lead, v in sorted(churn_by_lead.items()) if len(v) >= 30
        },
    }
    (DATA_DIR / "agent_stats.json").write_text(json.dumps(out, indent=1))
    print(f"agent_stats: {len(out['t2m_error_quantiles'])} t2m cells, "
          f"{len(out['rain_calibration'])} rain cells, "
          f"{len(out['churn_norms'])} churn leads", flush=True)


if __name__ == "__main__":
    sys.exit(main())
