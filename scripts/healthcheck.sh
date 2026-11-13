#!/usr/bin/env bash
# Exit non-zero if any critical component is unhealthy. Suitable for cron/monitoring.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
for svc in postgres redis api worker frontend nginx; do
  state=$(docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null | awk -v s="$svc" '$1==s {print $2" "$3}')
  if [[ "$state" != running* ]] || [[ "$state" == *unhealthy* ]]; then echo "FAIL $svc: ${state:-missing}"; fail=1; else echo "ok   $svc: $state"; fi
done
code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/health)
[[ "$code" == "200" ]] && echo "ok   https /api/health" || { echo "FAIL https /api/health -> $code"; fail=1; }
if [[ -f nginx/certs/fullchain.pem ]]; then
  end=$(openssl x509 -enddate -noout -in nginx/certs/fullchain.pem | cut -d= -f2)
  days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
  [[ $days -gt 14 ]] && echo "ok   TLS certificate: $days days left" || { echo "WARN TLS certificate expires in $days days"; fail=1; }
fi
df_pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[[ $df_pct -lt 85 ]] && echo "ok   disk ${df_pct}%" || { echo "WARN disk ${df_pct}%"; fail=1; }
exit $fail
