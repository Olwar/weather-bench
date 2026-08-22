"""Derived multi-source blends, stored as ordinary rows in `forecasts`.

Rationale: on the accrued data no single source wins everywhere. FMI's human
forecasters own day 0-2, AIFS owns day 3-7, and the plain unweighted mean of
every available source beats the BEST single source by 6-13% at day 0-5 - a
margin several times larger than the pre-registered AIFS-vs-Foreca effect.
This module makes that combination a first-class competitor so it is judged by
exactly the same machinery as everyone else, on matched samples, with no
special pleading.

Three blends, deliberately ordered by how much they can cheat:

  blend_mean    unweighted mean of every member available for that exact
                (city, run_time, target_time, var). ZERO fitted parameters, so
                it cannot overfit. This is the honest baseline.
  blend_ai      mean of AIFS deterministic + AIFS ensemble mean. A fixed subset
                picked a priori ("the two AI models"), also zero fitted
                parameters.
  blend_learned inverse-MAE weights per (var, lead day), refit continuously and
                fitted ONLY on target dates strictly before the issuing run's
                own UTC date. See the causality note on train_weights().

Member composition necessarily thins with lead: metno_nordic stops at ~day 2,
fmi_edited ~day 10, foreca ~day 11. A blend is therefore an average of whoever
is still publishing, which is exactly what a real service would have to do.
Rows are only emitted where >= MIN_MEMBERS members agree to show up, so a
"blend" is never secretly a single relabelled source.

Re-run after each collect; it rebuilds all blend rows from scratch and is
idempotent. Scoring days/local-day conventions stay entirely in score.py.
"""
import sys
from collections import defaultdict
from datetime import datetime, timezone

from common import get_db

MEMBERS = [
    "ecmwf_aifs025_single",
    "ecmwf_aifs_ens_mean",
    "ecmwf_ifs025",
    "best_match",
    "fmi_edited",
    "foreca",
    "metno_nordic",
]
AI_MEMBERS = ["ecmwf_aifs025_single", "ecmwf_aifs_ens_mean"]
# The blend a commercial product may actually serve: everything except Foreca,
# whose feed is scraped without a license. This is the estimator the live site
# ships, so IT is the one whose verified numbers the site must cite - quoting
# blend_mean (Foreca included) for a product that excludes Foreca would be
# claiming a different forecast's accuracy. Costs ~0.01 degC pooled 1-7.
OPEN_MEMBERS = [m for m in MEMBERS if m != "foreca"]
# wdir is deliberately absent: wind direction is circular, and a linear mean
# of 350 deg and 10 deg says south. Direction is scored per source, never blended.
VARS = ("t2m", "ws", "rain1h", "rh", "td", "gust", "pmsl", "cc", "snow")

MIN_MEMBERS = 2      # below this it is not a blend, it is one source with a new name
MIN_TRAIN_DAYS = 7   # distinct past target dates required before weights beat equal ones
EPS = 0.05           # floor on a member's MAE so one lucky cell cannot take all the weight


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _lead_day(run_time: str, target_time: str) -> int:
    return int((_utc(target_time) - _utc(run_time)).total_seconds() / 3600 // 24)


def wipe(con):
    con.execute("DELETE FROM forecasts WHERE source LIKE 'blend\\_%' ESCAPE '\\'")


def unweighted(con, name: str, members: list[str]) -> int:
    """Pure SQL so memory stays flat regardless of table size. Safe to select
    from `forecasts` while inserting into it: the inserted rows carry a
    blend_* source and so can never re-enter the member set."""
    marks = ",".join("?" * len(members))
    vmarks = ",".join("?" * len(VARS))
    cur = con.execute(
        f"""INSERT INTO forecasts(source, city, run_time, target_time, var, value)
            SELECT ?, city, run_time, target_time, var, AVG(value)
            FROM forecasts
            WHERE source IN ({marks}) AND var IN ({vmarks})
            GROUP BY city, run_time, target_time, var
            HAVING COUNT(*) >= ?""",
        [name, *members, *VARS, MIN_MEMBERS],
    )
    return cur.rowcount


def train_weights(con):
    """Cumulative per-member MAE by (var, lead day), keyed by the UTC date the
    weights become usable on.

    CAUSALITY: a run issued on date D may only use target dates <= D-1. Any
    sample whose target date is earlier than the run's date necessarily also has
    target_time < run_time, so the forecast being weighted is never allowed to
    learn from its own verification, nor from any hour that had not yet been
    observed when the forecast was issued. This is what makes blend_learned an
    honest competitor rather than a hindsight fit.
    """
    q = """
      SELECT f.source, f.var, f.run_time, f.target_time,
             ABS(f.value - o.value) AS err
      FROM forecasts f
      JOIN observations o
        ON o.city = f.city AND o.time = f.target_time AND o.var = f.var
      WHERE f.source IN (%s) AND f.var IN (%s) AND f.target_time >= f.run_time
    """ % (",".join("?" * len(MEMBERS)), ",".join("?" * len(VARS)))

    # (var, lead, source, target_date) -> [sum_err, n]
    acc = defaultdict(lambda: [0.0, 0])
    for source, var, run_time, target_time, err in con.execute(q, [*MEMBERS, *VARS]):
        acc[(var, _lead_day(run_time, target_time), source, target_time[:10])][0] += err
        acc[(var, _lead_day(run_time, target_time), source, target_time[:10])][1] += 1

    by_cell = defaultdict(lambda: defaultdict(dict))   # (var,lead) -> date -> source -> (sum,n)
    for (var, lead, source, date), (s, n) in acc.items():
        by_cell[(var, lead)][date][source] = (s, n)

    # running totals over dates in order -> "MAE using everything strictly earlier"
    weights = {}   # (var, lead) -> date -> {source: weight}
    for cell, per_date in by_cell.items():
        run_sum = defaultdict(float)
        run_n = defaultdict(int)
        seen_days = 0
        out = {}
        for date in sorted(per_date):
            if seen_days >= MIN_TRAIN_DAYS:
                w = {}
                for src, n in run_n.items():
                    if n:
                        w[src] = 1.0 / max(run_sum[src] / n, EPS)
                if w:
                    out[date] = w
            for src, (s, n) in per_date[date].items():
                run_sum[src] += s
                run_n[src] += n
            seen_days += 1
        weights[cell] = out
    return weights


def _weights_asof(weights, var, lead, run_date):
    """Latest weight vector strictly older than the issuing run's date."""
    per_date = weights.get((var, lead))
    if not per_date:
        return None
    usable = [d for d in per_date if d < run_date]
    return per_date[max(usable)] if usable else None


def learned(con, weights) -> int:
    run_times = [r[0] for r in con.execute("SELECT DISTINCT run_time FROM forecasts ORDER BY run_time")]
    marks = ",".join("?" * len(MEMBERS))
    vmarks = ",".join("?" * len(VARS))
    written = 0
    for run_time in run_times:
        run_date = run_time[:10]
        rows = defaultdict(dict)
        q = f"""SELECT source, city, target_time, var, value FROM forecasts
                WHERE run_time = ? AND source IN ({marks}) AND var IN ({vmarks})"""
        for source, city, target_time, var, value in con.execute(q, [run_time, *MEMBERS, *VARS]):
            rows[(city, target_time, var)][source] = value

        batch = []
        for (city, target_time, var), members in rows.items():
            if len(members) < MIN_MEMBERS or target_time < run_time:
                continue
            w = _weights_asof(weights, var, _lead_day(run_time, target_time), run_date)
            # No usable history yet -> fall back to equal weights, i.e. blend_mean.
            if w:
                pairs = [(v, w[s]) for s, v in members.items() if s in w]
                if len(pairs) < MIN_MEMBERS:
                    pairs = [(v, 1.0) for v in members.values()]
            else:
                pairs = [(v, 1.0) for v in members.values()]
            tot = sum(x for _, x in pairs)
            batch.append(("blend_learned", city, run_time, target_time, var,
                          sum(v * x for v, x in pairs) / tot))
        con.executemany(
            "INSERT OR REPLACE INTO forecasts(source,city,run_time,target_time,var,value)"
            " VALUES(?,?,?,?,?,?)", batch)
        written += len(batch)
    return written


def main():
    con = get_db()
    if "--wipe-only" in sys.argv:
        # Blend rows are derived and rebuilt nightly; between scoring runs they
        # are pure disk cost (~40% of DB growth), so the score service deletes
        # them when it is done. The daily backup runs before the next rebuild
        # and therefore stays blend-free and small.
        wipe(con)
        con.commit()
        print("blend rows wiped", flush=True)
        return
    print("rebuilding blends", flush=True)
    wipe(con)
    n1 = unweighted(con, "blend_mean", MEMBERS)
    print(f"  blend_mean    {n1:>9,} rows", flush=True)
    n2 = unweighted(con, "blend_ai", AI_MEMBERS)
    print(f"  blend_ai      {n2:>9,} rows", flush=True)
    n4 = unweighted(con, "blend_open", OPEN_MEMBERS)
    print(f"  blend_open    {n4:>9,} rows", flush=True)
    print("  training walk-forward weights...", flush=True)
    w = train_weights(con)
    n3 = learned(con, w)
    print(f"  blend_learned {n3:>9,} rows", flush=True)
    con.commit()
    print("done - now run score.py", flush=True)


if __name__ == "__main__":
    sys.exit(main())
