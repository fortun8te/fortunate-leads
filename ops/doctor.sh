#!/bin/bash
# One-shot health check for Fortunate Leads: prints PASS/WARN/FAIL/SKIP per check,
# exits 1 if anything FAILs (or WARNs, with --strict).
#
#   ops/doctor.sh            check everything
#   ops/doctor.sh --fix      first stop the old :8766 server and any hand-started :8777 one,
#                            (re)start the LaunchAgent, then check
#   ops/doctor.sh --quick    PRAGMA quick_check instead of the full integrity_check
#   ops/doctor.sh --strict   warnings also make the exit code non-zero
#   ops/doctor.sh --db PATH --port N   check another DB / port (defaults: data/leads.sqlite, 8777)
set -uo pipefail
# shellcheck source=ops/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fix=0 strict=0 quick=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fix) fix=1; shift ;;
    --strict) strict=1; shift ;;
    --quick) quick=1; shift ;;
    --db) FL_DB="${2:?--db needs a path}"; shift 2 ;;
    --port) FL_PORT="${2:?--port needs a number}"; shift 2 ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

LOG_WARN_BYTES=$((20 * 1024 * 1024))
WAL_WARN_BYTES=$((64 * 1024 * 1024))
DISK_FAIL_KB=$((1024 * 1024))       # 1 GB
DISK_WARN_KB=$((5 * 1024 * 1024))   # 5 GB
BACKUP_WARN_HOURS=36

npass=0 nwarn=0 nfail=0 nskip=0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c_pass=$'\033[32m' c_warn=$'\033[33m' c_fail=$'\033[31m' c_dim=$'\033[2m' c_off=$'\033[0m'
else
  c_pass='' c_warn='' c_fail='' c_dim='' c_off=''
fi

report() {   # report STATUS LABEL DETAIL
  local color=''
  case "$1" in
    PASS) npass=$((npass + 1)); color=$c_pass ;;
    WARN) nwarn=$((nwarn + 1)); color=$c_warn ;;
    FAIL) nfail=$((nfail + 1)); color=$c_fail ;;
    SKIP) nskip=$((nskip + 1)); color=$c_dim ;;
    INFO) color=$c_dim ;;
  esac
  printf '%s%-4s%s  %-14s %s\n' "$color" "$1" "$c_off" "$2" "$3"
}
pass() { report PASS "$@"; }
warn() { report WARN "$@"; }
fail() { report FAIL "$@"; }
skip() { report SKIP "$@"; }
info() { report INFO "$@"; }
# python helpers print "STATUS|label|detail" lines
report_lines() { while IFS='|' read -r st label detail; do [ -n "$st" ] && report "$st" "$label" "$detail"; done; }

describe_pid() { echo "pid $1: $(proc_cmd "$1")${2:+ (cwd $(proc_cwd "$1"))}"; }
is_old_project() { case "$(proc_cmd "$1") $(proc_cwd "$1")" in *Documents/Codex*) return 0 ;; esac; return 1; }

agent_pid='' agent_on=0
if is_macos && agent_loaded "$FL_LABEL"; then
  agent_on=1
  agent_pid="$(agent_field "$FL_LABEL" pid)"
fi

# --- --fix -----------------------------------------------------------------------
if [ "$fix" = 1 ]; then
  echo "== fix"
  old="$(port_pids "$FL_OLD_PORT")"
  if [ -n "$old" ]; then
    for pid in $old; do
      echo "stopping :$FL_OLD_PORT $(describe_pid "$pid" cwd)"
      is_old_project "$pid" && echo "  (old project server; close its Cursor session in ~/Documents/Codex or it comes back)"
    done
    # shellcheck disable=SC2086  # one PID per word
    stop_pids $old
  fi
  if ! is_macos; then
    echo "not macOS: LaunchAgent restart skipped"
  elif agent_loaded "$FL_LABEL"; then
    strays="$(port_pids "$FL_PORT" | grep -vx "${agent_pid:-none}")"
    if [ -n "$strays" ]; then
      echo "stopping hand-started :$FL_PORT server ${strays//$'\n'/ } (the agent needs the port)"
      # shellcheck disable=SC2086  # one PID per word
      stop_pids $strays
    fi
    echo "restarting $FL_LABEL"
    launchctl kickstart -k "$FL_DOMAIN/$FL_LABEL" || echo "kickstart failed"
  elif [ -f "$FL_AGENTS/$FL_LABEL.plist" ]; then
    echo "agent installed but not loaded: bootstrapping"
    launchctl enable "$FL_DOMAIN/$FL_LABEL"
    launchctl bootstrap "$FL_DOMAIN" "$FL_AGENTS/$FL_LABEL.plist" || echo "bootstrap failed"
  else
    echo "agent not installed: run $FL_OPS/install-launchagent.sh"
  fi
  if is_macos && agent_loaded "$FL_LABEL"; then
    wait_http /api/counts 15 || echo "server still not answering after 15 s"
    agent_on=1
    agent_pid="$(agent_field "$FL_LABEL" pid)"
  fi
  echo
fi

echo "== Fortunate Leads doctor  $(date '+%F %T')  repo $FL_REPO"

# --- platform / python -----------------------------------------------------------
if is_macos; then
  info platform "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"
else
  info platform "$(uname -s): launchd checks are skipped"
fi
if python_ok; then
  pass python "$FL_PYTHON $("$FL_PYTHON" -c 'import platform, sqlite3; print(platform.python_version(), "sqlite", sqlite3.sqlite_version)')"
else
  fail python "$FL_PYTHON missing, < 3.9 or no sqlite3 (xcode-select --install, or PYTHON=...)"
fi
have curl || fail curl "curl not found; HTTP checks cannot run"

# --- LaunchAgent -------------------------------------------------------------------
if ! is_macos || ! have launchctl; then
  skip agent "launchctl not available (not macOS)"
elif agent_loaded "$FL_LABEL"; then
  state="$(agent_field "$FL_LABEL" state)"
  last_exit="$(agent_field "$FL_LABEL" "last exit code")"
  plist="$FL_AGENTS/$FL_LABEL.plist"
  if [ "$state" = running ]; then
    pass agent "$FL_LABEL running, pid ${agent_pid:-?}, last exit ${last_exit:-n/a}"
  else
    fail agent "$FL_LABEL loaded but state=${state:-?}, last exit ${last_exit:-n/a} (see $FL_LOG; --fix)"
  fi
  if [ -f "$plist" ] && ! grep -qF "$FL_REPO/server/server.py" "$plist"; then
    warn agent-path "$plist does not point at $FL_REPO (re-run install-launchagent.sh)"
  fi
elif [ -f "$FL_AGENTS/$FL_LABEL.plist" ]; then
  fail agent "installed at $FL_AGENTS/$FL_LABEL.plist but not loaded (--fix)"
else
  fail agent "not installed (run $FL_OPS/install-launchagent.sh)"
fi

# --- ports -------------------------------------------------------------------------
listeners="$(port_pids "$FL_PORT")"
if ! have lsof; then
  skip "port $FL_PORT" "lsof not available"
elif [ -z "$listeners" ]; then
  fail "port $FL_PORT" "nothing listening"
else
  for pid in $listeners; do
    if [ "$agent_on" = 1 ] && [ "$pid" != "$agent_pid" ]; then
      fail "port $FL_PORT" "held by a non-agent process, $(describe_pid "$pid" cwd) (--fix)"
    else
      pass "port $FL_PORT" "$(describe_pid "$pid")"
    fi
  done
fi

old="$(port_pids "$FL_OLD_PORT")"
if ! have lsof; then
  skip "port $FL_OLD_PORT" "lsof not available"
elif [ -z "$old" ]; then
  pass "port $FL_OLD_PORT" "free (old server not running)"
else
  for pid in $old; do
    if is_old_project "$pid"; then
      fail "port $FL_OLD_PORT" "OLD PROJECT server from ~/Documents/Codex: $(describe_pid "$pid" cwd). Close that Cursor session, then --fix"
    else
      fail "port $FL_OLD_PORT" "$(describe_pid "$pid" cwd) (--fix stops it)"
    fi
  done
fi

# --- HTTP ----------------------------------------------------------------------------
server_up=0
if have curl && counts="$(curl -fsS -m 5 "http://127.0.0.1:$FL_PORT/api/counts" 2>&1)"; then
  server_up=1
  pass server "http://127.0.0.1:$FL_PORT/api/counts $(printf '%s' "$counts" | "$FL_PYTHON" -c '
import json, sys
c = json.load(sys.stdin)
print("total=%s with_bio=%s hot=%s warm=%s" % tuple(c.get(k) for k in ("total", "with_bio", "hot", "warm")))' 2>/dev/null)"
else
  fail server "http://127.0.0.1:$FL_PORT/api/counts not answering (${counts:-no curl})"
fi

# --- database ------------------------------------------------------------------------
if [ ! -f "$FL_DB" ]; then
  fail database "no DB at $FL_DB"
else
  wal=0
  [ -f "$FL_DB-wal" ] && wal="$(file_size "$FL_DB-wal")"
  info database "$FL_DB $(human_bytes "$(file_size "$FL_DB")")"
  if [ "$wal" -gt "$WAL_WARN_BYTES" ]; then
    warn wal "$(human_bytes "$wal") (checkpoints not keeping up; a long-lived reader? restart the agent)"
  else
    pass wal "$(human_bytes "$wal")"
  fi
  # (captured, not piped: report_lines must run in this shell to update the counters)
  db_report="$("$FL_PYTHON" - "$FL_DB" "$quick" <<'PY'
import sqlite3, sys, time, urllib.parse
path, quick = sys.argv[1], sys.argv[2] == '1'
try:
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(path), uri=True, timeout=30)
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1')
    except sqlite3.Error:
        conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    print(('PASS' if mode == 'wal' else 'WARN') + '|journal|journal_mode=' + mode)
    t = time.time()
    pragma = 'quick_check' if quick else 'integrity_check'
    rows = [r[0] for r in conn.execute('PRAGMA ' + pragma)]
    took = '%.1fs' % (time.time() - t)
    if rows == ['ok']:
        print('PASS|integrity|%s ok (%s)' % (pragma, took))
    else:
        print('FAIL|integrity|%s: %s%s (restore from data/backups)' % (pragma, '; '.join(rows[:3]),
                                                                       ' ...' if len(rows) > 3 else ''))
except sqlite3.Error as e:
    print('FAIL|integrity|cannot read DB: %s' % e)
PY
)"
  report_lines <<<"$db_report"
fi

# --- disk ------------------------------------------------------------------------------
dbdir="$(dirname "$FL_DB")"
[ -d "$dbdir" ] || dbdir="$FL_REPO"
avail_kb="$(df -Pk "$dbdir" 2>/dev/null | awk 'NR == 2 {print $4}')"
if [ -z "$avail_kb" ]; then
  warn disk "df failed for $dbdir"
elif [ "$avail_kb" -lt "$DISK_FAIL_KB" ]; then
  fail disk "$(human_bytes $((avail_kb * 1024))) free on $dbdir"
elif [ "$avail_kb" -lt "$DISK_WARN_KB" ]; then
  warn disk "$(human_bytes $((avail_kb * 1024))) free on $dbdir"
else
  pass disk "$(human_bytes $((avail_kb * 1024))) free on $dbdir"
fi

# --- extension / scraper -------------------------------------------------------------------
if [ "$server_up" = 1 ] && scraper="$(curl -fsS -m 5 "http://127.0.0.1:$FL_PORT/api/scraper" 2>/dev/null)"; then
  ext_report="$(printf '%s' "$scraper" | "$FL_PYTHON" -c '
import json, socket, sys
from datetime import datetime, timezone

s = json.load(sys.stdin)
manifest = sys.argv[1]
now = datetime.now(timezone.utc)

def when(ts):
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None

def ago(sec):
    sec = abs(int(sec))
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if sec >= n:
            return "%d %s" % (sec // n, unit)
    return "%d s" % sec

def out(st, label, detail):
    print("%s|%s|%s" % (st, label, str(detail).replace("\n", " ")))

ext = s.get("ext") or {}
try:
    repo_ver = json.load(open(manifest)).get("version")
except (OSError, ValueError):
    repo_ver = None
seen = when(ext.get("last_seen"))
ver = ext.get("version") or "?"
if seen is None:
    out("WARN", "extension", "no heartbeat ever (extension not installed/loaded in Chrome?)")
else:
    age = (now - seen).total_seconds()
    line = "v%s, heartbeat %s ago, state %s" % (ver, ago(age), ext.get("state") or "?")
    if ext.get("online"):
        out("PASS", "extension", line)
    else:
        out("WARN", "extension", line + " (offline: Chrome closed, no instagram.com tab, or extension asleep)")
if repo_ver and ext.get("version") and ext["version"] != repo_ver:
    out("WARN", "ext-version", "Chrome runs v%s, repo manifest is v%s: reload the extension" % (ext["version"], repo_ver))

cd = when(ext.get("cooldown_until"))
if cd and cd > now:
    out("WARN", "cooldown", "until %s (%s left); Instagram limit hit, it resumes by itself" % (ext["cooldown_until"], ago((cd - now).total_seconds())))
else:
    out("PASS", "cooldown", "none")
if s.get("paused"):
    out("WARN", "paused", "scraper paused (UI or POST /api/scraper/pause {\"paused\":false})")

q = s.get("queue") or {}
lists = s.get("lists") or []
states = {}
for l in lists:
    states[l.get("state")] = states.get(l.get("state"), 0) + 1
out("INFO", "queue", "jobs: list %s, profile %s | lists: %s" % (q.get("list", 0), q.get("profile", 0),
    ", ".join("%s %d" % kv for kv in sorted(states.items(), key=lambda kv: str(kv[0]))) or "none"))
errs = [l for l in lists if l.get("state") == "error"]
if errs:
    out("WARN", "list-errors", "; ".join("@%s %s: %s" % (l.get("seed"), l.get("direction"), l.get("error")) for l in errs[:3])
        + (" (+%d more)" % (len(errs) - 3) if len(errs) > 3 else ""))

today, budget = ext.get("today") or {}, ext.get("budget") or {}
for kind in ("list", "profile"):
    used, cap = today.get(kind) or 0, budget.get(kind) or 0
    if cap and used >= 0.9 * cap:
        out("WARN", "budget", "%s %s/%s today (near the daily cap)" % (kind, used, cap))

if ext.get("last_error"):
    out("WARN", "last-error", ext["last_error"])
else:
    out("PASS", "last-error", "none")

soak = (s.get("soak") or {}).get("1h") or {}
rate = ext.get("rate") or {}
out("INFO", "last-hour", "pages %s, people %s, bios %s; ext rate %s pages/h" % (
    soak.get("pages", 0), soak.get("people", 0), soak.get("profiles", 0), rate.get("pages_hour", "n/a")))

if s.get("qualify"):
    try:
        socket.create_connection(("127.0.0.1", 18741), timeout=1).close()
        out("PASS", "qualify", "on; LLM proxy :18741 reachable")
    except OSError:
        out("WARN", "qualify", "on, but LLM proxy :18741 is down: rule verdicts only")
else:
    out("INFO", "qualify", "off" + (" (switches on by itself when lists finish)" if s.get("qualify_auto") else ""))
' "$FL_REPO/extension/manifest.json")"
  report_lines <<<"$ext_report"
elif [ "$server_up" = 1 ]; then
  fail scraper "/api/scraper failed"
else
  skip extension "server down: heartbeat, cooldown, queue, last error not checked"
fi

# --- log -----------------------------------------------------------------------------------------
if [ -f "$FL_LOG" ]; then
  size="$(file_size "$FL_LOG")"
  if [ "$size" -gt "$LOG_WARN_BYTES" ]; then
    warn log "$FL_LOG is $(human_bytes "$size"); ops/backup.sh rotates it (install --with-backup to do it daily)"
  else
    pass log "$FL_LOG $(human_bytes "$size")"
  fi
  tb="$(tail -n 400 "$FL_LOG" | grep -c '^Traceback' || true)"
  if [ "${tb:-0}" -gt 0 ]; then
    last="$(tail -n 400 "$FL_LOG" | grep -E '^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception)' | tail -n 1 | cut -c1-160)"
    warn log-errors "$tb traceback(s) in the last 400 lines; last: ${last:-?}"
  fi
elif is_macos; then
  warn log "no $FL_LOG yet (agent never started?)"
else
  skip log "$FL_LOG (macOS only)"
fi

# --- backups ---------------------------------------------------------------------------------------
bdir="$(dirname "$FL_DB")/backups"
newest="$(find "$bdir" -maxdepth 1 -name 'leads-*.sqlite' -type f 2>/dev/null | sort | tail -n 1)"
if [ -z "$newest" ]; then
  warn backup "none in $bdir (run ops/backup.sh; install --with-backup for daily)"
else
  count="$(find "$bdir" -maxdepth 1 -name 'leads-*.sqlite' -type f | wc -l | tr -d ' ')"
  age_h="$("$FL_PYTHON" -c 'import os, sys, time; print(int((time.time() - os.path.getmtime(sys.argv[1])) // 3600))' "$newest")"
  if [ "$age_h" -gt "$BACKUP_WARN_HOURS" ]; then
    warn backup "newest $(basename "$newest") is ${age_h} h old ($count kept)"
  else
    pass backup "newest $(basename "$newest"), ${age_h} h old, $count kept"
  fi
fi
if is_macos && have launchctl; then
  if agent_loaded "$FL_BACKUP_LABEL"; then
    pass backup-agent "$FL_BACKUP_LABEL loaded (daily 03:30), last exit $(agent_field "$FL_BACKUP_LABEL" "last exit code")"
  else
    warn backup-agent "not installed (install-launchagent.sh --with-backup)"
  fi
else
  skip backup-agent "launchctl not available"
fi

# --- summary ----------------------------------------------------------------------------------------
echo "== $npass pass, $nwarn warn, $nfail fail, $nskip skip"
if [ "$nfail" -gt 0 ]; then
  [ "$fix" = 0 ] && echo "try: $0 --fix"
  exit 1
fi
[ "$strict" = 1 ] && [ "$nwarn" -gt 0 ] && exit 1
exit 0
