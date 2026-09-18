#!/usr/bin/env bash
#
# Bring up the ARIA extraction service on the VM.
#
#   ./ai-api-wrapper/deploy_vm.sh setup     one-time: venv, deps, .env, verification
#   ./ai-api-wrapper/deploy_vm.sh start     run in the background
#   ./ai-api-wrapper/deploy_vm.sh status    is it up, and is storage reachable
#   ./ai-api-wrapper/deploy_vm.sh stop      stop it
#   ./ai-api-wrapper/deploy_vm.sh logs      follow the log
#   ./ai-api-wrapper/deploy_vm.sh key       print ARIA_API_KEY (to give the web app team)
#   ./ai-api-wrapper/deploy_vm.sh check     storage access check, writes nothing
#   ./ai-api-wrapper/deploy_vm.sh all       setup + start
#
# Needs no root. Safe to re-run: every step is idempotent and nothing is overwritten
# without --force.
#
#
# CREDENTIAL MODEL (proof of concept)
# -----------------------------------
# Managed identity is not being granted, so:
#
#   reads                -> ANONYMOUS. Both containers allow public read; verified with a
#                           plain curl returning HTTP 200 and no credential.
#   writes               -> AZURE_STORAGE_KEY. Anonymous PUT returns HTTP 401.
#   AZURE_STORAGE_ACCOUNT-> not a secret; it appears in every job URL
#   blob URLs            -> arrive in the job payload at runtime, never configured here
#   ARIA_API_KEY         -> guards THIS service's /jobs endpoint. Generated below.
#                           NOT the storage key: this one is shared with the web app team.
#
# `.env` is generated on the VM, chmod 600, and never committed.
#
#
# WHAT SETUP REFUSES TO PROCEED WITHOUT
# -------------------------------------
# A write credential -- see check_write_credential.
#
# Not managed identity: its absence is expected here and is reported for information only.
# The gate is on the key because reads succeed anonymously, so a missing key lets a job
# download its inputs, run the whole extraction, and only then fail at the upload. That burns
# the run and looks like a pipeline bug rather than a configuration one.
#
# If an identity is ever assigned, set USE_MANAGED_IDENTITY=1 in .env and remove the key.
# That path is still in the code and is the better option: no secret on disk.

set -euo pipefail

# ─────────────────────────────────────────────────────────────── settings

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_DIR/ai-api-wrapper"

# Named `wrapper_venv`, not `.venv`: this VM is shared and the repo root may hold other
# teammates' environments (there are already api/ and ui/ directories). A name that says
# which service it belongs to prevents someone installing into the wrong one.
# Ignored via `wrapper_venv/` in .gitignore -- check that entry survives if the venv is
# ever relocated, or git will try to track thousands of files.
VENV_NAME="${VENV_NAME:-wrapper_venv}"
VENV="$REPO_DIR/$VENV_NAME"
PY="$VENV/bin/python"
ENV_FILE="$APP_DIR/.env"
LOG_FILE="$HOME/aria-api.log"
PID_FILE="$HOME/aria-api.pid"

STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-sabiohackteam3}"
PORT="${PORT:-8001}"
HOST="${HOST:-0.0.0.0}"          # 0.0.0.0, not 127.0.0.1: the web app connects from off-box
MIN_PY_MINOR=10                  # 3.10+: FastAPI resolves `str | None` annotations at runtime

IMDS_URL="http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://storage.azure.com/"

FORCE=0
SKIP_IDENTITY=0

# ──────────────────────────────────────────────────────────────── output

if [ -t 1 ]; then
    R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
    R=; G=; Y=; B=; N=
fi
say()  { printf '%s\n' "$*"; }
head_() { printf '\n%s== %s ==%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %sok%s   %s\n' "$G" "$N" "$*"; }
warn() { printf '  %swarn%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %sFAIL%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# ───────────────────────────────────────────────────────────────── steps

find_python() {
    # An explicit PYTHON= wins. RHEL/CentOS ship Python 3.6 as the system `python3` and it
    # cannot be replaced without root, so the practical route on a locked-down VM is a
    # user-local interpreter (uv or pyenv) pointed at with:
    #     PYTHON=~/.local/bin/python3.12 ./deploy_vm.sh setup
    local c best="${PYTHON:-}"
    if [ -n "$best" ]; then
        command -v "$best" >/dev/null 2>&1 || die "PYTHON=$best is not executable"
    else
        for c in python3.13 python3.12 python3.11 python3.10 python3; do
            command -v "$c" >/dev/null 2>&1 || continue
            best="$c"; break
        done
    fi
    [ -n "$best" ] || die "no python3 found. Ask Engineer 1/2 to install Python 3.$MIN_PY_MINOR+"

    local minor
    minor="$("$best" -c 'import sys; print(sys.version_info[1])')"
    if [ "$("$best" -c 'import sys; print(sys.version_info[0])')" -lt 3 ] \
       || [ "$minor" -lt "$MIN_PY_MINOR" ]; then
        die "$("$best" --version) is too old; need 3.$MIN_PY_MINOR+.

       RHEL/CentOS ship 3.6 as the system python3. Two ways forward:

       NO ROOT NEEDED -- install a user-local Python with uv:
           curl -LsSf https://astral.sh/uv/install.sh | sh
           ~/.local/bin/uv python install 3.12
           PYTHON=\$(~/.local/bin/uv python find 3.12) $0 setup

       WITH ROOT -- ask Engineer 1/2:
           sudo dnf install -y python3.12 python3.12-devel   # RHEL/Rocky 8-9
           then re-run: $0 setup"
    fi
    printf '%s' "$best"
}

check_identity() {
    # Advisory only. Managed identity is not being granted for this POC, so its absence is
    # expected and must not block the deploy. It was a hard gate while identity was the
    # intended path; now the credential model is:
    #     reads  -> anonymous, both containers allow public read
    #     writes -> AZURE_STORAGE_KEY (checked by check_write_credential below)
    head_ "managed identity (optional)"
    if [ "$SKIP_IDENTITY" = 1 ]; then
        warn "--skip-identity-check: skipped"
        return 0
    fi
    local body
    body="$(curl -s -H 'Metadata:true' --max-time 8 "$IMDS_URL" 2>/dev/null || true)"
    case "$body" in
        *access_token*)
            ok "assigned -- you can switch to it with USE_MANAGED_IDENTITY=1 in .env"
            ok "that is the better long-term option: no key on disk, nothing to rotate" ;;
        *)
            ok "not assigned -- expected for this POC, using the account key instead" ;;
    esac
}

check_write_credential() {
    # THE gate that actually matters now. Reads are anonymous so they always work; a missing
    # key means every job runs the full extraction and then fails at the upload, which wastes
    # the run and reads like a pipeline bug.
    head_ "write credential"
    if grep -q '^AZURE_STORAGE_KEY=.\+' "$ENV_FILE" 2>/dev/null; then
        ok "AZURE_STORAGE_KEY present in $ENV_FILE"
    elif [ -n "${AZURE_STORAGE_KEY:-}" ]; then
        ok "AZURE_STORAGE_KEY present in the environment"
    else
        die "no AZURE_STORAGE_KEY.

       Reads are anonymous, so downloads will work -- but every job will fail at the
       point it uploads its result, after doing all the work.

       Add it to $ENV_FILE:
           AZURE_STORAGE_KEY=<the 88-character account key>

       Keep the file chmod 600. It must NOT be committed, and it must never be reused
       as ARIA_API_KEY -- that value is shared with the web app team."
    fi
}

make_venv() {
    head_ "python environment"
    local py; py="$(find_python)"
    ok "using $("$py" --version) ($(command -v "$py"))"

    if [ -x "$PY" ]; then
        ok "venv already exists at $VENV"
    else
        "$py" -m venv "$VENV" || die "could not create a venv.
       On RHEL/CentOS this usually means python3-venv is missing -- ask Engineer 1/2."
        ok "created $VENV"
    fi

    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" \
        || die "dependency install failed (see output above)"
    ok "dependencies installed from ai-api-wrapper/requirements.txt"
}

write_env() {
    head_ "configuration"
    if [ -f "$ENV_FILE" ] && [ "$FORCE" = 0 ]; then
        # Never silently rotate the key: the web app is holding a copy, and changing it here
        # would 401 every request they make without anyone knowing why.
        ok "$ENV_FILE exists -- left untouched (pass --force to regenerate)"
    else
        [ -f "$ENV_FILE" ] && warn "--force: regenerating; ARIA_API_KEY CHANGES, reshare it"
        "$PY" - "$ENV_FILE" "$STORAGE_ACCOUNT" "${AZURE_STORAGE_KEY:-}" <<'PY'
import secrets, sys, pathlib
path, account, key = sys.argv[1], sys.argv[2], sys.argv[3]
pathlib.Path(path).write_text(
    "# Generated on the VM by deploy_vm.sh. NOT from git, NOT committed, chmod 600.\n"
    "#\n"
    "# POC credential model:\n"
    "#   reads  -> anonymous. Both containers allow public read; no credential needed.\n"
    "#   writes -> AZURE_STORAGE_KEY below. Required, or jobs fail at the upload step.\n"
    f"AZURE_STORAGE_ACCOUNT={account}\n"
    "\n"
    "# 0 = use the account key (this POC). 1 = use the VM's managed identity, which is the\n"
    "# better option if it is ever assigned: no key on disk, nothing to rotate.\n"
    "USE_MANAGED_IDENTITY=0\n"
    "\n"
    "# The 88-character storage account key. Needed for UPLOADS only.\n"
    f"AZURE_STORAGE_KEY={key}\n"
    "\n"
    "# Guards THIS service's /jobs endpoint. Share with the web app team.\n"
    "# This is NOT the storage account key and must never be set to it: this value is\n"
    "# handed out, and the storage key grants full control of the whole account.\n"
    f"ARIA_API_KEY={secrets.token_urlsafe(32)}\n"
    "\n"
    "LOG_LEVEL=INFO\n"
    "MAX_CONCURRENT_JOBS=2\n"
)
PY
        chmod 600 "$ENV_FILE"
        ok "wrote $ENV_FILE with a fresh ARIA_API_KEY"
        if [ -z "${AZURE_STORAGE_KEY:-}" ]; then
            warn "AZURE_STORAGE_KEY was left blank -- fill it in before starting:"
            warn "    \$EDITOR $ENV_FILE"
            warn "  or re-run as:  AZURE_STORAGE_KEY=<key> $0 setup --force"
        else
            ok "AZURE_STORAGE_KEY taken from the environment"
        fi
    fi
    chmod 600 "$ENV_FILE"
    ok "permissions 600 (other accounts on this VM cannot read it)"
}

storage_check() {
    head_ "storage access (read-only, writes nothing)"
    local job="$APP_DIR/jobs.json"
    [ -f "$job" ] || { warn "no $job to check against -- skipping"; return 0; }
    if "$PY" "$APP_DIR/run_job.py" --job "$job" --env-file "$ENV_FILE" --check; then
        ok "storage reachable"
    else
        warn "the check failed. Common causes:"
        warn "  * role assignments missing (identity exists but has no data access)"
        warn "  * the blobs named in jobs.json no longer exist"
        warn "The service can still start; individual jobs will report their own errors."
    fi
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_service() {
    head_ "starting the service"
    if is_running; then
        ok "already running (pid $(cat "$PID_FILE")) -- 'stop' first to restart"
        return 0
    fi
    [ -x "$PY" ] || die "no venv yet. Run: $0 setup"

    # cd to the repo root: uvicorn resolves ai-api-wrapper.api as a module path from here.
    cd "$REPO_DIR"
    nohup "$PY" -m uvicorn ai-api-wrapper.api:app \
          --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 5

    if ! is_running; then
        rm -f "$PID_FILE"
        say ""
        tail -20 "$LOG_FILE"
        die "the service exited during startup -- see the log above and $LOG_FILE"
    fi
    ok "running (pid $(cat "$PID_FILE")) on $HOST:$PORT"
    say "  log: $LOG_FILE"

    if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        ok "/health responds"
        local auth
        auth="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/ready" 2>/dev/null \
                | sed -n 's/.*"auth":"\([^"]*\)".*/\1/p')"
        [ -n "$auth" ] && ok "credential in use: $auth"
        if [ "$auth" != "DefaultAzureCredential" ] && [ -n "$auth" ]; then
            warn "expected DefaultAzureCredential on a VM; USE_MANAGED_IDENTITY may be 0"
        fi
    else
        warn "/health did not respond yet -- check $LOG_FILE"
    fi
}

stop_service() {
    head_ "stopping the service"
    if ! is_running; then
        ok "not running"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid; pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "did not stop gracefully; sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    ok "stopped"
}

show_status() {
    head_ "status"
    if is_running; then
        ok "running (pid $(cat "$PID_FILE"))"
    else
        warn "not running"
    fi
    say "  repo   : $REPO_DIR"
    say "  venv   : $VENV  ($([ -x "$PY" ] && echo present || echo MISSING))"
    say "  .env   : $([ -f "$ENV_FILE" ] && echo present || echo MISSING)"
    say "  log    : $LOG_FILE"
    say "  listen : $HOST:$PORT"
    if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/ready" 2>/dev/null; then
        say ""
    else
        warn "/ready not responding on 127.0.0.1:$PORT"
    fi
}

show_key() {
    [ -f "$ENV_FILE" ] || die "no $ENV_FILE yet. Run: $0 setup"
    head_ "ARIA_API_KEY"
    say "  Give this to the web app team. Send it via Key Vault, a password manager or an"
    say "  internal ticket -- not chat or email. It is NOT the storage account key."
    say ""
    grep '^ARIA_API_KEY=' "$ENV_FILE" | cut -d= -f2-
}

next_steps() {
    local key; key="$(grep '^ARIA_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    head_ "next steps"
    cat <<EOF
  Test from inside the VM:
      curl -s localhost:$PORT/health
      curl -s localhost:$PORT/ready

  Submit a job:
      KEY=\$($0 key | tail -1)
      curl -s -X POST localhost:$PORT/jobs \\
        -H "X-API-Key: \$KEY" -H "Content-Type: application/json" \\
        -d @ai-api-wrapper/jobs.json

  STILL NEEDED FROM INFRA
    1. NSG rule allowing inbound $PORT -- restricted to the web app's subnet,
       not the internet. This VM has a public IP.
    2. Host firewall, if firewalld is active:  sudo firewall-cmd --add-port=$PORT/tcp
    3. To survive reboot:  loginctl enable-linger \$USER   (then use systemctl --user)

  STILL NEEDED FROM THE TEAM
    4. run_pipeline() in ai-api-wrapper/aria_pipeline.py is a DUMMY. Until the AI team
       fills it in, a successful job produces a placeholder workbook, flagged in the
       manifest as "implementation": "DUMMY" with needs_review: true.

  NOT BLOCKERS ANY MORE
    * managed identity -- not needed; reads are anonymous and writes use the key
    * Resource Master  -- uploaded to ariallminput/resource-master/Resource Master.xlsx
EOF
}

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ────────────────────────────────────────────────────────────────── main

CMD=""
for arg in "$@"; do
    case "$arg" in
        --force)                FORCE=1 ;;
        --skip-identity-check)  SKIP_IDENTITY=1 ;;
        -h|--help)              usage 0 ;;
        -*)                     die "unknown option: $arg" ;;
        *)                      [ -z "$CMD" ] && CMD="$arg" || die "unexpected: $arg" ;;
    esac
done
[ -n "$CMD" ] || usage 0

case "$CMD" in
    setup)
        say "${B}ARIA extraction service -- setup${N}"
        say "repo: $REPO_DIR"
        check_identity
        make_venv
        write_env
        check_write_credential
        storage_check
        next_steps
        say ""
        say "Now start it:  $0 start"
        ;;
    start)  start_service; say ""; say "Stop with:  $0 stop" ;;
    stop)   stop_service ;;
    status) show_status ;;
    logs)   [ -f "$LOG_FILE" ] || die "no log at $LOG_FILE"; tail -f "$LOG_FILE" ;;
    key)    show_key ;;
    check)  [ -x "$PY" ] || die "no venv yet. Run: $0 setup"; storage_check ;;
    all)
        say "${B}ARIA extraction service -- setup and start${N}"
        check_identity
        make_venv
        write_env
        check_write_credential
        storage_check
        start_service
        next_steps
        ;;
    restart) stop_service; start_service ;;
    *)      die "unknown command: $CMD  (try: $0 --help)" ;;
esac
