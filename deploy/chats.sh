#!/bin/sh
# Print Ilma chat conversations from data/chat_log.jsonl, grouped by visitor.
# Usage on the VPS: /opt/weather-bench/deploy/chats.sh [YYYY-MM-DD]   (default: today, "all" for everything)
D=${1:-$(date -u +%F)}
F=/opt/weather-bench/data/chat_log.jsonl
[ -f "$F" ] || { echo "no chats logged yet"; exit 0; }
python3 - "$F" "$D" <<'PY'
import json, sys, collections
f, day = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(f) if l.strip()]
if day != "all": rows = [r for r in rows if r["t"].startswith(day)]
by = collections.OrderedDict()
for r in rows: by.setdefault(r["who"], []).append(r)
if not rows: print("no chats on", day)
for who, rs in by.items():
    tin = sum((r["tokens"].get("in") or 0) for r in rs); tout = sum((r["tokens"].get("out") or 0) for r in rs)
    print(f"\n=== visitor {who} · {rs[0]['t'][:16]}Z → {rs[-1]['t'][11:16]}Z · {len(rs)} rounds · tokens in {tin} out {tout}")
    for r in rs:
        if r.get("user"):
            u = r["user"].split("[context:")[0].strip()
            print(f"  YOU  {u}")
        if r.get("tool_calls"):
            print("  TOOL " + "; ".join(f"{c['name']} {c['args']}" for c in r["tool_calls"]))
        if r.get("reply"):
            print("  ILMA " + r["reply"].strip().replace("\n", "\n       "))
PY
