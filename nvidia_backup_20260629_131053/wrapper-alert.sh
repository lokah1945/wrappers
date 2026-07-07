#!/usr/bin/env bash
# wrapper-nvidia alerts (lightweight, no Prometheus required).
# Compares /health + /metrics against thresholds, prints alert lines to stdout.
# Returns non-zero exit if any CRITICAL alert is firing (cron-friendly).
#
# Usage: wrapper-alert.sh [--exit-on-critical] [--json]
set -uo pipefail

HOST="${WRAPPER_HOST:-127.0.0.1}"
PORT="${WRAPPER_PORT:-9100}"
BASE="http://${HOST}:${PORT}"

CRIT_ON_EXIT=0
JSON_OUT=0
for arg in "$@"; do
  case "$arg" in
    --exit-on-critical) CRIT_ON_EXIT=1 ;;
    --json)             JSON_OUT=1 ;;
    -h|--help)
      echo "Usage: $0 [--exit-on-critical] [--json]"; exit 0 ;;
  esac
done

curl_max="--max-time 3"
curl_body="$(curl -sSf $curl_max "${BASE}/metrics/prom" 2>/dev/null || true)"

if [[ -z "$curl_body" ]]; then
  msg="[CRITICAL] wrapper-nvidia: service not reachable at ${BASE}"
  if [[ "$JSON_OUT" = "1" ]]; then
    echo "{\"severity\":\"critical\",\"alert\":\"unreachable\",\"host\":\"${HOST}\",\"port\":${PORT}}"
  else
    echo "$msg"
  fi
  [[ "$CRIT_ON_EXIT" = "1" ]] && exit 1
  exit 0
fi

# parse key counters
keys_total=$(echo "$curl_body"   | awk -F' ' '/^wrapper_nvidia_keys_total / {print $2}')
keys_avail=$(echo "$curl_body"   | awk -F' ' '/^wrapper_nvidia_keys_available / {print $2}')
keys_block=$(echo "$curl_body"   | awk -F' ' '/^wrapper_nvidia_keys_blocked / {print $2}')
rpm_total=$(echo "$curl_body"    | awk -F' ' '/^wrapper_nvidia_rpm_total / {print $2}')
in_flight=$(echo "$curl_body"    | awk -F' ' '/^wrapper_nvidia_in_flight_total / {print $2}')
latency_ms=$(echo "$curl_body"   | awk -F' ' '/^wrapper_nvidia_avg_latency_ms_24h / {print $2}')
exhaust24h=$(echo "$curl_body"   | awk -F' ' '/^wrapper_nvidia_exhaustions_total_24h / {print $2}')

ALERTS=()

if [[ "${keys_avail:-0}" = "0" && "${keys_total:-0}" -gt 0 ]]; then
  ALERTS+=("critical|all_keys_exhausted|available=${keys_avail}/total=${keys_total}")
fi

if [[ "${keys_total:-0}" -gt 0 ]]; then
  blocked_pct=$(awk -v b="$keys_block" -v t="$keys_total" 'BEGIN{if(t>0) printf "%.0f", (b*100)/t; else print 0}')
  if [[ "${blocked_pct:-0}" -ge 80 && "${keys_avail:-0}" -gt 0 ]]; then
    ALERTS+=("high|high_key_blocking|blocked=${keys_block}/${keys_total} (${blocked_pct}%)")
  fi
fi

if awk -v v="${latency_ms:-0}" 'BEGIN{exit !(v+0>8000)}'; then
  ALERTS+=("medium|high_latency|24h_avg=${latency_ms}ms (threshold=8000ms)")
fi

if awk -v e="${exhaust24h:-0}" 'BEGIN{exit !(e+0>50)}'; then
  ALERTS+=("high|exhaustion_spike|exhaustions_24h=${exhaust24h}")
fi

if [[ "${in_flight:-0}" -gt 50 ]]; then
  ALERTS+=("high|high_inflight|in_flight=${in_flight}")
fi

if [[ "${#ALERTS[@]}" -eq 0 ]]; then
  if [[ "$JSON_OUT" = "1" ]]; then
    echo "{\"status\":\"ok\",\"keys_total\":${keys_total},\"keys_available\":${keys_avail},\"avg_latency_ms\":${latency_ms:-0}}"
  else
    printf "OK wrapper-nvidia [%s keys, avg_latency_ms=%s]\n" \
      "${keys_avail:-?}/${keys_total:-?}" "${latency_ms:-?}"
  fi
  exit 0
fi

if [[ "$JSON_OUT" = "1" ]]; then
  ALERTS_JSON="$(printf '{"severity":"%s","name":"%s","detail":"%s"},' "${ALERTS[@]/%/*}" "${ALERTS[@]/%/*}" "${ALERTS[@]}" | sed 's/,$//' || true)"
  echo "{\"status\":\"alert\",\"alerts\":[$ALERTS_JSON]}"
else
  for a in "${ALERTS[@]}"; do
    severity="${a%%|*}"
    rest="${a#*|}"
    name="${rest%%|*}"
    detail="${rest#*|}"
    printf "%s %-22s %s\n" "[${severity^^}]" "$name" "$detail"
  done
fi

# exit non-zero if any critical alert present
if [[ "$CRIT_ON_EXIT" = "1" ]]; then
  for a in "${ALERTS[@]}"; do
    [[ "${a%%|*}" = "critical" ]] && exit 1
  done
fi
exit 0
