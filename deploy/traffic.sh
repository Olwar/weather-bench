#!/bin/sh
# Traffic summary for ilma.io from the origin's nginx log (log_format "ilma":
#   client [time] "req" status bytes request_time upstream_time "referer" "agent" via=proxy_ip)
# Usage on the VPS: /opt/weather-bench/deploy/traffic.sh [hours]   (default 24)
# For a full dashboard: goaccess /var/log/nginx/weather-bench.access.log --log-format='%h [%d:%t %^] "%r" %s %b %T %^ "%R" "%u" %^' --date-format=%d/%b/%Y --time-format=%T
H=${1:-24}
L=/var/log/nginx/weather-bench.access.log
since=$(date -u -d "-$H hour" +%s)
awk -v since="$since" -v H="$H" '
function ts(t,  a,m){ split(t,a,"[/:]"); m=(index("JanFebMarAprMayJunJulAugSepOctNovDec",a[2])+2)/3;
  return mktime(a[3]" "m" "a[1]" "a[4]" "a[5]" "a[6]) }
$2 ~ /^\[/ {
  if (ts(substr($2,2)) < since) next;
  n++; ip[$1]++; p=$5; sub(/\?.*/,"",p); path[p]++; st[$7]++;
  if ($7==429) r429++; if (p=="/api/agent/chat" && $7==200) chat++;
  if ($9+0 > 5) slow++; if ($1==substr($NF,5)) direct++;
  if (p ~ /^\/api\//) api++; else if (p=="/") page++; else other++ }
END {
  printf "last %s h: %d requests (%d api, %d page, %d other), %d unique clients, %d chat answers, %d rate-limited, %d slow >5s, %d direct-to-origin\n", H, n, api, page, other, length(ip), chat, r429, slow, direct;
  printf "status:"; for (s in st) printf " %s=%d", s, st[s]; print "";
  print "top paths:"; for (k in path) printf "%6d %s\n", path[k], k | "sort -rn | head -12"; close("sort -rn | head -12");
  print "top clients:"; for (k in ip) printf "%6d %s\n", ip[k], k | "sort -rn | head -8"; close("sort -rn | head -8");
}' "$L"
