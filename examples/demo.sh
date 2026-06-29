#!/usr/bin/env bash
# Threshly end-to-end demo — no GPU required.
#
# Brings up a coordinator + two mock workers, submits a batch, KILLS a worker mid-run to simulate a
# spot preemption, and shows the batch still completing exactly once. Run from the repo root:
#
#     bash examples/demo.sh
#
set -euo pipefail

PORT="${PORT:-8080}"
BASE="http://localhost:${PORT}"
N="${N:-600}"
WORKDIR="$(mktemp -d)"
PY="${PY:-python}"
export THRESHLY_DATABASE_URL="sqlite:///${WORKDIR}/threshly.db"
export THRESHLY_BLOB_DIR="${WORKDIR}/blobs"
export THRESHLY_LEASE_SECONDS=3       # short reclaim deadline so the demo is snappy
export THRESHLY_REAPER_INTERVAL=1

pids=()
cleanup() {
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "==> starting coordinator on ${BASE}"
threshly coordinator --port "$PORT" >"${WORKDIR}/coord.log" 2>&1 &
pids+=($!)
sleep 3

echo "==> starting two mock workers (one with a large lease so a kill leaves work in flight)"
threshly worker --coordinator "$BASE" --engine mock --lease-size 64 >"${WORKDIR}/w1.log" 2>&1 &
W1=$!; pids+=($W1)
threshly worker --coordinator "$BASE" --engine mock --lease-size 16 >"${WORKDIR}/w2.log" 2>&1 &
pids+=($!)
sleep 1

echo "==> generating ${N} requests (all sharing one system prompt => prefix-cache friendly)"
"$PY" examples/gen_sample.py "$N" >"${WORKDIR}/batch.jsonl"

echo "==> submitting batch"
BID="$(threshly submit "${WORKDIR}/batch.jsonl" --model demo-model --coordinator "$BASE" 2>/dev/null | tail -1)"
echo "    batch id: ${BID}"

sleep 2
echo "==> SIMULATING SPOT PREEMPTION: kill -9 worker ${W1} mid-run"
kill -9 "$W1" || true

echo "==> waiting for completion (watch the reclaim + exactly-once result)..."
while :; do
  read -r STATUS DONE FAILED < <(curl -s "${BASE}/v1/batches/${BID}" \
    | "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d['request_counts']['completed'],d['request_counts']['failed'])")
  RECLAIMED="$(curl -s "${BASE}/metrics" | awk '/^threshly_leases_reclaimed_total /{print $2}')"
  printf '\r    status=%-12s completed=%-4s failed=%-3s reclaimed=%s   ' "$STATUS" "$DONE" "$FAILED" "${RECLAIMED:-0}"
  case "$STATUS" in completed|failed|cancelled) echo; break;; esac
  sleep 1
done

threshly results "$BID" -o "${WORKDIR}/out.jsonl" --coordinator "$BASE" >/dev/null
LINES="$(grep -c . "${WORKDIR}/out.jsonl")"
UNIQUE="$("$PY" -c "import json;print(len({json.loads(l)['custom_id'] for l in open('${WORKDIR}/out.jsonl')}))")"
echo "==> output: ${LINES} results, ${UNIQUE} unique custom_ids (expected ${N})"

echo "==> final metrics:"
curl -s "${BASE}/metrics" | grep -E "threshly_(requests_completed_total|prefix_cache_(hits|misses)_total|leases_reclaimed_total|estimated_cost_usd) " | grep -v '#'

if [ "$LINES" = "$N" ] && [ "$UNIQUE" = "$N" ]; then
  echo "==> SUCCESS: a worker was killed mid-run, yet every request completed exactly once."
else
  echo "==> MISMATCH: expected ${N}, got ${LINES}/${UNIQUE}"; exit 1
fi
