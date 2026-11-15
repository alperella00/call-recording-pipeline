#!/usr/bin/env bash
# Failure drill: run the whole system, break it in the ways the README warns about, and check after each
# scenario that every recording finished and the CRM has at most one booking and one complaint per call.
#
#   scripts/drill.sh                 # every scenario, in order
#   scripts/drill.sh kill crm_down   # only these
#
# Scenarios: kill kill_all scale bad_key key_breaks crm_down db_restart up_again db_reset
#
# It wipes the local stack (docker compose down -v) between scenarios. CHAOS=1 throughout.
# ANTHROPIC_API_KEY is cleared, so extraction uses the rule-based fallback (no LLM cost).
# Speech-to-text: the stack's own configuration. With the real ElevenLabs key every fresh start transcribes
# every recording again; in development we run it with STT_PROVIDER=replay.
# The bad-key scenarios use a wrong ElevenLabs key, which is rejected before any credits are used.

set -euo pipefail
cd "$(dirname "$0")/.."

export CHAOS=1 ANTHROPIC_API_KEY=
DC=(docker compose)
FAILURES=0

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
sql()  { "${DC[@]}" exec -T db psql -U postgres -d callpipe -At -c "$1"; }
status() { "${DC[@]}" run --rm -q cli app.status "$@"; }

fresh() {   # empty database and empty CRMs, then the one command from the README
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  "${DC[@]}" up -d --build >/dev/null 2>&1
}

wait_for() {   # wait until a SQL count is > 0
  local query=$1 limit=${2:-300} t=0
  until [ "$(sql "$query")" -gt 0 ] 2>/dev/null; do
    sleep 2; t=$((t + 2))
    if [ $t -ge "$limit" ]; then note "timed out waiting for: $query"; return 1; fi
  done
  return 0
}

crm_counts() {   # "bookings complaints" in brightsmile's CRM
  python3 - <<'EOF'
import json, urllib.request
get = lambda p: json.load(urllib.request.urlopen(f"http://localhost:8080/{p}"))
print(len(get("bookings")["bookings"]), len(get("complaints")["complaints"]))
EOF
}

check() {   # every recording finished, nothing failed (unless allowed), no duplicates in the CRM
  local label=$1 allow_failed=${2:-no}
  local out ok=yes
  out=$(status --wait 900) || ok=no
  echo "$out" | sed 's/^/   /'
  [ "$allow_failed" = no ] && echo "$out" | grep -q "failed [1-9]" && ok=no
  python3 - <<'EOF' || ok=no
import collections, json, sys, urllib.request
get = lambda p: json.load(urllib.request.urlopen(f"http://localhost:8080/{p}"))
for kind in ("bookings", "complaints"):
    items = get(kind)[kind]
    dup = {c: n for c, n in collections.Counter(i["call_id"] for i in items).items() if n > 1}
    print(f"   CRM {kind}: {len(items)}, duplicates: {dup or 'none'}")
    if dup:
        sys.exit(1)
EOF
  local raw; raw=$(sql "SELECT count(*) FROM raw_transcripts")
  note "raw transcripts left in the database: $raw"
  [ "$allow_failed" = no ] && [ "$raw" != 0 ] && ok=no
  if [ $ok = yes ]; then note "PASS: $label"; else note "FAIL: $label"; FAILURES=$((FAILURES + 1)); fi
}

workers() { docker ps -q --filter "label=com.docker.compose.service=worker" --filter "label=com.docker.compose.project=callpipe"; }

kill_random_worker() {   # SIGKILL one running worker, keeping at least one alive unless told otherwise
  local keep=${1:-1} victim
  [ "$(workers | wc -l)" -gt "$keep" ] || return 0
  victim=$(workers | sort -R | head -1)
  docker kill -s KILL "$victim" >/dev/null && note "killed worker ${victim:0:12} ($(workers | wc -l | tr -d ' ') left)"
}

# --- Scenarios --------------------------------------------------------------------------------------

scenario_kill() {
  say "kill: SIGKILL workers during processing, including during CRM writes"
  fresh
  sleep 4; kill_random_worker
  wait_for "SELECT count(*) FROM jobs WHERE step = 'crm' AND status = 'running'" 300 && kill_random_worker
  note "the remaining worker takes over the killed workers' jobs"
  check "killed workers: their jobs are taken over, nothing is lost or sent twice"
}

scenario_kill_all() {
  say "kill_all: every worker is killed; nothing moves until they are started again"
  fresh
  wait_for "SELECT count(*) FROM jobs WHERE step = 'crm'" 300
  while [ -n "$(workers)" ]; do kill_random_worker 0; done
  sleep 5
  note "jobs left unfinished, none lost: $(sql "SELECT count(*) FROM jobs WHERE status <> 'done'")"
  note "docker compose up -d (killed containers are not restarted by Docker: a kill counts as a manual stop)"
  "${DC[@]}" up -d >/dev/null 2>&1
  check "after starting the workers again, everything finishes without duplicates"
}

scenario_scale() {
  say "scale: 5 workers, then 1"
  fresh
  "${DC[@]}" up -d --scale worker=5 --no-recreate worker >/dev/null 2>&1
  sleep 10
  "${DC[@]}" up -d --scale worker=1 --no-recreate worker >/dev/null 2>&1
  check "scaling up and down mid-run"
  "${DC[@]}" up -d --scale worker=3 --no-recreate worker >/dev/null 2>&1
}

scenario_bad_key() {
  say "bad_key: wrong speech-to-text key from the start, then fixed and reprocessed"
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  # The real provider with a wrong key (the development override would replay saved responses instead).
  ELEVENLABS_API_KEY=wrong-key STT_PROVIDER=elevenlabs \
    docker compose -f docker-compose.yml up -d --build >/dev/null 2>&1
  check "every recording fails at transcribe with a clear reason" allow_failed
  sql "SELECT DISTINCT last_error FROM jobs" | sed 's/^/   reason: /'
  [ "$(sql "SELECT count(*) FROM jobs WHERE status = 'failed' AND step = 'transcribe'")" -gt 0 ] \
    || { note "FAIL: expected failed(transcribe)"; FAILURES=$((FAILURES + 1)); }
  note "fixing the key (restoring the normal configuration) and reprocessing"
  "${DC[@]}" up -d worker >/dev/null 2>&1
  "${DC[@]}" run --rm -q cli app.reprocess | tail -1 | sed 's/^/   /'
  check "after the key is fixed, reprocess finishes everything"
}

scenario_key_breaks() {
  say "key_breaks: the speech-to-text key breaks mid-run"
  # One worker, so recordings are transcribed one after another and the break lands in the middle.
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  "${DC[@]}" up -d --build --scale worker=1 >/dev/null 2>&1
  wait_for "SELECT count(*) FROM jobs WHERE step <> 'transcribe' HAVING count(*) >= 3" 300
  ELEVENLABS_API_KEY=wrong-key STT_PROVIDER=elevenlabs \
    docker compose -f docker-compose.yml up -d --no-deps --scale worker=1 worker >/dev/null 2>&1
  note "key broken after $(sql "SELECT count(*) FROM jobs WHERE step <> 'transcribe'") recordings were transcribed"
  check "recordings already transcribed finish; the rest fail at transcribe" allow_failed
  local done_before failed_before
  done_before=$(sql "SELECT count(*) FROM jobs WHERE status = 'done'")
  failed_before=$(sql "SELECT count(*) FROM jobs WHERE status = 'failed' AND step = 'transcribe'")
  [ "$failed_before" -gt 0 ] || { note "FAIL: nothing failed at transcribe"; FAILURES=$((FAILURES + 1)); }
  "${DC[@]}" up -d worker >/dev/null 2>&1
  "${DC[@]}" run --rm -q cli app.reprocess | tail -1 | sed 's/^/   /'
  check "reprocess resumes only the $failed_before failed ones ($done_before were done)"
}

scenario_crm_down() {
  say "crm_down: the CRM is stopped during the CRM step, then started again"
  fresh
  wait_for "SELECT count(*) FROM jobs WHERE step = 'crm'" 300
  "${DC[@]}" stop crm-mock >/dev/null 2>&1
  note "CRM stopped"
  status --wait 900 | sed 's/^/   /' || true
  local failed; failed=$(sql "SELECT count(*) FROM jobs WHERE status = 'failed' AND step = 'crm'")
  note "failed at the CRM step while it was down: $failed"
  sql "SELECT DISTINCT last_error FROM jobs WHERE status = 'failed'" | sed 's/^/   reason: /'
  [ "$failed" -gt 0 ] || { note "FAIL: expected failed(crm)"; FAILURES=$((FAILURES + 1)); }
  "${DC[@]}" start crm-mock >/dev/null 2>&1
  sleep 5
  "${DC[@]}" run --rm -q cli app.reprocess | tail -1 | sed 's/^/   /'
  check "after the CRM is back, reprocess finishes without duplicates"
}

scenario_db_restart() {
  say "db_restart: Postgres restarts mid-run"
  fresh
  wait_for "SELECT count(*) FROM jobs WHERE status = 'done'" 300
  note "unfinished when the database restarts: $(sql "SELECT count(*) FROM jobs WHERE status <> 'done'")"
  "${DC[@]}" restart db >/dev/null 2>&1
  note "database restarted"
  check "workers reconnect (restarted by docker) and continue"
  for c in $("${DC[@]}" ps -q worker api); do
    note "$(docker inspect "$c" --format '{{.Name}}: restarted {{.RestartCount}} time(s)')"
  done
}

scenario_up_again() {
  say "up_again: the start command is run again on a finished system"
  local before after
  before=$(crm_counts)
  "${DC[@]}" up -d --build >/dev/null 2>&1
  sleep 20
  after=$(crm_counts)
  note "CRM bookings/complaints before: $before, after: $after"
  [ "$before" = "$after" ] || { note "FAIL: new CRM writes"; FAILURES=$((FAILURES + 1)); }
  check "nothing new is written"
}

scenario_db_reset() {
  say "db_reset: the database is deleted, the CRM keeps its data, everything runs again"
  local before; before=$(crm_counts)
  "${DC[@]}" down >/dev/null 2>&1
  docker volume rm callpipe_db_data >/dev/null
  "${DC[@]}" up -d --build >/dev/null 2>&1
  check "existing CRM entries are recognised by call_id, not created again"
  note "CRM bookings/complaints before the reset: $before, after: $(crm_counts)"
}

ALL=(kill kill_all scale bad_key key_breaks crm_down db_restart up_again db_reset)
for s in "${@:-${ALL[@]}}"; do "scenario_$s"; done

say "drill finished: $FAILURES failure(s)"
exit $((FAILURES > 0))
