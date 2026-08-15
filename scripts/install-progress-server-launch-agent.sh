#!/bin/bash
# Install the Tailnet progress receiver as a per-user macOS LaunchAgent.

set -euo pipefail
umask 077

readonly LABEL="com.ryutek.oraja-training.progress-server"
readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
readonly DEFAULT_PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

HOST=""
PORT="8765"
PROJECT_ROOT="$DEFAULT_PROJECT_ROOT"
PYTHON_BIN=""
EXPORT_DIR=""
PROGRESS_STATE=""
TOKEN_FILE=""
UNINSTALL=0

usage() {
    cat <<'EOF'
Usage:
  install-progress-server-launch-agent.sh --host TAILSCALE_IP [options]
  install-progress-server-launch-agent.sh --uninstall

Options:
  --host IP                    Exact local Tailscale IPv4 or IPv6 address
  --port PORT                  Listening port (default: 8765)
  --project-root DIR           Repository root (default: script parent)
  --python PATH                Project Python (default: PROJECT/.venv/bin/python)
  --export-dir DIR             Export root (default: PROJECT/export/current)
  --progress-state PATH        State file (default: PROJECT/runtime/progress.json)
  --progress-token-file PATH   Existing 0600 token file
  --uninstall                  Remove only this LaunchAgent and stop its job
  --help                       Show this help
EOF
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --host)
            (($# >= 2)) || fail "--host requires a value"
            HOST="$2"
            shift 2
            ;;
        --port)
            (($# >= 2)) || fail "--port requires a value"
            PORT="$2"
            shift 2
            ;;
        --project-root)
            (($# >= 2)) || fail "--project-root requires a value"
            PROJECT_ROOT="$2"
            shift 2
            ;;
        --python)
            (($# >= 2)) || fail "--python requires a value"
            PYTHON_BIN="$2"
            shift 2
            ;;
        --export-dir)
            (($# >= 2)) || fail "--export-dir requires a value"
            EXPORT_DIR="$2"
            shift 2
            ;;
        --progress-state)
            (($# >= 2)) || fail "--progress-state requires a value"
            PROGRESS_STATE="$2"
            shift 2
            ;;
        --progress-token-file)
            (($# >= 2)) || fail "--progress-token-file requires a value"
            TOKEN_FILE="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[[ "$(uname -s)" == "Darwin" ]] || fail "this installer supports macOS only"

readonly USER_ID="$(id -u)"
readonly USER_HOME="${HOME:?HOME is not set}"
[[ "$USER_HOME" == /* ]] || fail "HOME must be an absolute path"
readonly AGENT_DIR="$USER_HOME/Library/LaunchAgents"
readonly PLIST_PATH="$AGENT_DIR/$LABEL.plist"
readonly DOMAIN_TARGET="gui/$USER_ID"
readonly SERVICE_TARGET="$DOMAIN_TARGET/$LABEL"
readonly LAUNCHCTL_ATTEMPTS=5
readonly LAUNCHCTL_RETRY_SECONDS=1

job_is_ours() {
    local description="$1"
    if printf '%s\n' "$description" | grep -Fq "path = $PLIST_PATH"; then
        return 0
    fi
    printf '%s\n' "$description" | grep -Fq "type = Submitted" \
        && printf '%s\n' "$description" | grep -Fq "oraja_training.cli" \
        && printf '%s\n' "$description" | grep -Fq -- "--export-dir" \
        && printf '%s\n' "$description" | grep -Fq -- "--host" \
        && printf '%s\n' "$description" | grep -Fq -- "--progress-state" \
        && printf '%s\n' "$description" | grep -Fq -- "--progress-token-file"
}

stop_existing_job() {
    local description
    if ! description="$(/bin/launchctl print "$SERVICE_TARGET" 2>/dev/null)"; then
        return 0
    fi
    job_is_ours "$description" \
        || fail "refusing to replace an unrecognized job with label $LABEL"
    /bin/launchctl bootout "$SERVICE_TARGET"
}

wait_until_job_is_unregistered() {
    local attempt
    for ((attempt = 1; attempt <= LAUNCHCTL_ATTEMPTS; attempt++)); do
        if ! /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
            return 0
        fi
        if ((attempt < LAUNCHCTL_ATTEMPTS)); then
            /bin/sleep "$LAUNCHCTL_RETRY_SECONDS"
        fi
    done
    return 1
}

bootstrap_launch_agent() {
    local attempt
    local last_error=""
    for ((attempt = 1; attempt <= LAUNCHCTL_ATTEMPTS; attempt++)); do
        if /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
            return 0
        fi
        if last_error="$(
            /bin/launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_PATH" 2>&1
        )"; then
            if /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
                return 0
            fi
        fi
        if ((attempt < LAUNCHCTL_ATTEMPTS)); then
            /bin/sleep "$LAUNCHCTL_RETRY_SECONDS"
        fi
    done
    if /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
        return 0
    fi
    if [[ -n "$last_error" ]]; then
        printf 'last launchctl error: %s\n' "$last_error" >&2
    fi
    return 1
}

print_bootstrap_recovery() {
    printf 'Recovery commands:\n' >&2
    printf '  /bin/launchctl bootout %q 2>/dev/null || true\n' \
        "$SERVICE_TARGET" >&2
    printf '  /bin/launchctl bootstrap %q %q\n' \
        "$DOMAIN_TARGET" "$PLIST_PATH" >&2
    printf '  /bin/launchctl kickstart %q\n' "$SERVICE_TARGET" >&2
}

if ((UNINSTALL)); then
    if [[ -L "$PLIST_PATH" ]]; then
        fail "refusing to remove a symbolic-link LaunchAgent: $PLIST_PATH"
    fi
    if [[ -e "$PLIST_PATH" ]]; then
        [[ -f "$PLIST_PATH" ]] || fail "LaunchAgent path is not a file: $PLIST_PATH"
    fi
    stop_existing_job
    if [[ -e "$PLIST_PATH" ]]; then
        /bin/rm -f "$PLIST_PATH"
    fi
    printf 'Removed %s\n' "$LABEL"
    exit 0
fi

[[ -n "$HOST" ]] || fail "--host is required"
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "--port must be an integer"
PORT="$((10#$PORT))"
((PORT >= 1 && PORT <= 65535)) || fail "--port must be between 1 and 65535"

absolute_existing_path() {
    local candidate="$1"
    if [[ "$candidate" != /* ]]; then
        candidate="$PWD/$candidate"
    fi
    local parent
    parent="$(CDPATH= cd -- "$(dirname -- "$candidate")" && pwd -P)" \
        || fail "path parent does not exist: $candidate"
    printf '%s/%s\n' "$parent" "$(basename -- "$candidate")"
}

PROJECT_ROOT="$(absolute_existing_path "$PROJECT_ROOT")"
[[ -d "$PROJECT_ROOT" ]] || fail "project root is not a directory: $PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
PYTHON_BIN="$(absolute_existing_path "$PYTHON_BIN")"
[[ -x "$PYTHON_BIN" ]] || fail "project Python is not executable: $PYTHON_BIN"

canonical_path() {
    "$PYTHON_BIN" -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' \
        "$1"
}

PROJECT_ROOT="$(canonical_path "$PROJECT_ROOT")"
EXPORT_DIR="$(canonical_path "${EXPORT_DIR:-$PROJECT_ROOT/export/current}")"
PROGRESS_STATE="$(canonical_path "${PROGRESS_STATE:-$PROJECT_ROOT/runtime/progress.json}")"
TOKEN_FILE="$(canonical_path "${TOKEN_FILE:-$PROJECT_ROOT/runtime/progress-token.txt}")"
readonly LOG_FILE="$PROJECT_ROOT/runtime/progress-server.log"

[[ -d "$EXPORT_DIR" ]] || fail "export directory is missing: $EXPORT_DIR"
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] \
    || fail "token must be an existing regular file: $TOKEN_FILE"

token_owner="$(/usr/bin/stat -f '%u' "$TOKEN_FILE")"
token_mode="$(/usr/bin/stat -f '%Lp' "$TOKEN_FILE")"
token_size="$(/usr/bin/stat -f '%z' "$TOKEN_FILE")"
[[ "$token_owner" == "$USER_ID" ]] || fail "token must be owned by the current user"
(( (8#$token_mode & 077) == 0 )) || fail "token permissions must deny group/other access"
((token_size >= 1 && token_size <= 4096)) || fail "token file size is invalid"

TAILSCALE_BIN="$(command -v tailscale || true)"
if [[ -z "$TAILSCALE_BIN" && -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    TAILSCALE_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
[[ -n "$TAILSCALE_BIN" ]] || fail "tailscale command was not found"

normalize_ip() {
    "$PYTHON_BIN" -c \
        'import ipaddress, sys; print(ipaddress.ip_address(sys.argv[1]))' "$1"
}

HOST="$(normalize_ip "$HOST")" || fail "--host must be an IP address"
tailscale_v4="$("$TAILSCALE_BIN" ip -4 2>/dev/null || true)"
tailscale_v6="$("$TAILSCALE_BIN" ip -6 2>/dev/null || true)"
host_matches=0
while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    if [[ "$(normalize_ip "$candidate" 2>/dev/null || true)" == "$HOST" ]]; then
        host_matches=1
    fi
done <<< "$tailscale_v4"$'\n'"$tailscale_v6"
((host_matches)) \
    || fail "--host does not match this Mac's tailscale ip -4/-6 output"

state_parent="$(dirname -- "$PROGRESS_STATE")"
/bin/mkdir -p "$state_parent" "$PROJECT_ROOT/runtime" "$AGENT_DIR"
if [[ -e "$PROGRESS_STATE" ]]; then
    [[ -f "$PROGRESS_STATE" && ! -L "$PROGRESS_STATE" ]] \
        || fail "progress state is not a regular file: $PROGRESS_STATE"
    [[ "$(/usr/bin/stat -f '%u' "$PROGRESS_STATE")" == "$USER_ID" ]] \
        || fail "progress state must be owned by the current user"
    /bin/chmod 600 "$PROGRESS_STATE"
fi
/usr/bin/touch "$LOG_FILE"
/bin/chmod 600 "$LOG_FILE"

[[ ! -L "$PLIST_PATH" ]] || fail "refusing to overwrite a symbolic-link LaunchAgent"
if [[ -e "$PLIST_PATH" && ! -f "$PLIST_PATH" ]]; then
    fail "LaunchAgent path is not a regular file: $PLIST_PATH"
fi

candidate_plist="$(/usr/bin/mktemp "$AGENT_DIR/.$LABEL.XXXXXX")"
cleanup() {
    if [[ -n "${candidate_plist:-}" && -e "$candidate_plist" ]]; then
        /bin/rm -f "$candidate_plist"
    fi
}
trap cleanup EXIT HUP INT TERM

"$PYTHON_BIN" - "$candidate_plist" "$PYTHON_BIN" "$PROJECT_ROOT" \
    "$EXPORT_DIR" "$HOST" "$PORT" "$PROGRESS_STATE" "$TOKEN_FILE" "$LOG_FILE" <<'PY'
import plistlib
from pathlib import Path
import sys

(
    output,
    python,
    project_root,
    export_dir,
    host,
    port,
    progress_state,
    token_file,
    log_file,
) = sys.argv[1:]

for value in (python, project_root, export_dir, progress_state, token_file, log_file):
    if not Path(value).is_absolute():
        raise SystemExit(f"LaunchAgent path is not absolute: {value}")

payload = {
    "Label": "com.ryutek.oraja-training.progress-server",
    "Program": python,
    "ProgramArguments": [
        python,
        "-m",
        "oraja_training.cli",
        "serve",
        "--export-dir",
        export_dir,
        "--host",
        host,
        "--port",
        port,
        "--progress-state",
        progress_state,
        "--progress-token-file",
        token_file,
    ],
    "WorkingDirectory": project_root,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "Umask": 0o077,
    "ProcessType": "Background",
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
}
with open(output, "wb") as destination:
    plistlib.dump(payload, destination, fmt=plistlib.FMT_XML, sort_keys=False)
PY

/bin/chmod 600 "$candidate_plist"
/usr/bin/plutil -lint "$candidate_plist" >/dev/null

existing_description=""
if existing_description="$(/bin/launchctl print "$SERVICE_TARGET" 2>/dev/null)"; then
    job_is_ours "$existing_description" \
        || fail "refusing to replace an unrecognized job with label $LABEL"
fi

/bin/mv -f "$candidate_plist" "$PLIST_PATH"
candidate_plist=""
/bin/chmod 600 "$PLIST_PATH"

if [[ -n "$existing_description" ]]; then
    /bin/launchctl bootout "$SERVICE_TARGET"
    if ! wait_until_job_is_unregistered; then
        printf 'error: %s remained registered after bootout\n' "$LABEL" >&2
        print_bootstrap_recovery
        exit 1
    fi
fi
if ! bootstrap_launch_agent; then
    printf 'error: failed to bootstrap %s after %s attempts\n' \
        "$LABEL" "$LAUNCHCTL_ATTEMPTS" >&2
    print_bootstrap_recovery
    exit 1
fi
/bin/launchctl kickstart "$SERVICE_TARGET"
/bin/launchctl print "$SERVICE_TARGET" >/dev/null

printf 'Installed %s for %s:%s\n' "$LABEL" "$HOST" "$PORT"
