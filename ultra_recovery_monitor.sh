#!/bin/bash
# Ultra recovery monitor: probe NVIDIA upstream (direct) + wrapper every 120s.
# Logs one line per cycle. Stops after MAX cycles or when healthy 3x in a row.
cd /root/wrapper
TOKEN=$(grep -oP '^BEARER_TOKEN=\K.*' nvidia-python/.env)
K1=$(grep -oP '^NVIDIA_API_KEY_1=\K.*' nvidia-python/.env)
M="nvidia/nemotron-3-ultra-550b-a55b"
LOG=/root/wrapper/ultra_recovery.log
MAX=60   # 60 cycles * 120s = 2 hours max
HEALTHY=0
: > "$LOG"
for i in $(seq 1 $MAX); do
  TS=$(date '+%F %T')
  # direct upstream probe
  DIRECT=$(curl -s -m 45 -o /tmp/u_direct.json -w '%{http_code}' https://integrate.api.nvidia.com/v1/chat/completions \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $K1" \
    -d "{\"model\":\"$M\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")
  DIRECT_T=$(cat /tmp/u_direct.json | head -c 120 | tr '\n' ' ')
  # wrapper probe (anthropic surface)
  WRAP=$(curl -s -m 60 -o /tmp/u_wrap.json -w '%{http_code}' http://localhost:9101/v1/messages \
    -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' -H "Authorization: Bearer $TOKEN" \
    -d "{\"model\":\"$M\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")
  WRAP_T=$(head -c 120 /tmp/u_wrap.json | tr '\n' ' ')
  echo "$TS cycle=$i direct=$DIRECT wrap=$WRAP | direct_body=$DIRECT_T | wrap_body=$WRAP_T" >> "$LOG"
  if [ "$WRAP" = "200" ]; then
    HEALTHY=$((HEALTHY+1))
  else
    HEALTHY=0
  fi
  if [ "$HEALTHY" -ge 3 ]; then
    echo "$TS RECOVERED (3 consecutive healthy) — monitor exiting" >> "$LOG"
    exit 0
  fi
  sleep 120
done
echo "$(date '+%F %T') monitor timeout after $MAX cycles" >> "$LOG"
