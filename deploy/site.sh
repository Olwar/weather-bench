#!/bin/sh
# Deploy the site: regenerate SEO pages, copy static to the Hetzner origin, deploy Vercel.
set -e
cd "$(dirname "$0")/.."
python3 web/seo.py
rsync -a --delete --exclude '.DS_Store' web/static/ root@89.167.5.149:/opt/weather-bench/web/static/
. ~/.claude/.env 2>/dev/null || true
vercel deploy --prod --yes --token "$VERCEL_TOKEN" | grep -E "Aliased|Error"
