#!/usr/bin/env bash
#
# Sentinel Platform — one-command installer for Debian/Ubuntu VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo bash
#
# What it does (idempotent — safe to re-run):
#   1. Installs system packages (python3.12 venv, git, curl).
#   2. Clones/updates the repo into /opt/sentinel.
#   3. Creates a dedicated system user + virtualenv + dependencies.
#   4. Prompts for the 4 required secrets and writes /opt/sentinel/.env (chmod 600).
#   5. Installs + enables the systemd unit, then verifies the platform booted.
#
# Non-interactive / automated use — export these first:
#   SENTINEL_API_ID, SENTINEL_API_HASH, CONTROL_BOT_TOKEN, SENTINEL_OWNER_ID
#   (and optionally SENTINEL_NONINTERACTIVE=1 to fail instead of prompting)
#
set -euo pipefail

REPO_URL="${SENTINEL_REPO_URL:-https://github.com/mhm-hossein/sentinel.git}"
BRANCH="${SENTINEL_BRANCH:-main}"
INSTALL_DIR="${SENTINEL_INSTALL_DIR:-/opt/sentinel}"
SERVICE_USER="${SENTINEL_SERVICE_USER:-sentinel}"
SERVICE_NAME="${SENTINEL_SERVICE_NAME:-sentinel}"
PY_BIN="${SENTINEL_PYTHON:-python3}"

C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_OFF"; }

# ---------------------------------------------------------------- pre-flight
[ "$(id -u)" -eq 0 ] || die "Run as root:  curl -fsSL ... | sudo bash"
command -v systemctl >/dev/null 2>&1 || die "systemd not found — this installer targets Debian/Ubuntu VPS hosts."

# `curl ... | sudo bash` feeds the script itself over stdin, so any prompt must
# read from the controlling terminal explicitly instead of stdin.
TTY_DEV=""
if [ -t 0 ]; then
  TTY_DEV="/dev/stdin"
elif [ -r /dev/tty ] && [ -t 1 ]; then
  TTY_DEV="/dev/tty"
fi

if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) : ;;
    *) warn "Detected '${PRETTY_NAME:-unknown}' — continuing, but only Debian/Ubuntu is tested." ;;
  esac
fi

say "Sentinel installer"
dim "repo:    $REPO_URL ($BRANCH)"
dim "target:  $INSTALL_DIR"
dim "service: $SERVICE_NAME (user: $SERVICE_USER)"

# ------------------------------------------------------------ system packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS="git curl ca-certificates python3 python3-venv python3-pip"
apt-get install -y -qq $PKGS >/dev/null

# Run a command as the service user. Minimal VPS images ship without `sudo`, so
# prefer `runuser` (util-linux, always present) and fall back to sudo/setpriv.
as_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$SERVICE_USER" -H -- "$@"
  elif command -v setpriv >/dev/null 2>&1; then
    setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_USER" --clear-groups "$@"
  else
    su -s /bin/sh "$SERVICE_USER" -c "$(printf '%q ' "$@")"
  fi
}

# Prefer a 3.12+ interpreter (the project targets 3.12; 3.11 also works).
pick_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}
if [ "$PY_BIN" = "python3" ]; then
  PY_BIN="$(pick_python || true)"
fi
[ -n "$PY_BIN" ] || die "Python 3.11+ not found. On Ubuntu 22.04: apt install -y python3.12 python3.12-venv (deadsnakes PPA)."
PY_VER="$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
dim "python:  $PY_BIN ($PY_VER)"

# --------------------------------------------------------------- service user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating system user '$SERVICE_USER'"
  useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ---------------------------------------------------------------- get the code
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating existing checkout in $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
  say "Cloning repository"
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ------------------------------------------------------------------ virtualenv
say "Creating virtualenv + installing dependencies"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  as_user env "HOME=$INSTALL_DIR" "$PY_BIN" -m venv "$INSTALL_DIR/.venv"
fi
# The service user owns the dir but may lack $HOME for pip's cache; point it at
# a writable temp dir so a locked-down account can't break the install.
PIP_ENV=(env "HOME=$INSTALL_DIR" PIP_DISABLE_PIP_VERSION_CHECK=1)
as_user "${PIP_ENV[@]}" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel -q
as_user "${PIP_ENV[@]}" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
dim "deps:    telethon, aiosqlite, python-dotenv, aiohttp, pypdf, python-docx, psutil"

# --------------------------------------------------------------------- .env
ENV_FILE="$INSTALL_DIR/.env"

ask() {  # ask <VAR> <prompt> <secret:0|1>
  local var="$1" prompt="$2" secret="${3:-0}" current value
  current="$(sed -n "s/^${var}=//p" "$ENV_FILE" 2>/dev/null | head -1)"
  if [ -n "${!var:-}" ]; then
    value="${!var}"
  elif [ -n "$current" ] && ! printf '%s' "$current" | grep -qx -e '123456' -e 'your_api_hash_here' -e ''; then
    dim "$var already configured — keeping it"
    return 0
  elif [ "${SENTINEL_NONINTERACTIVE:-0}" = "1" ] || [ -z "$TTY_DEV" ]; then
    die "$var is required — export it first, or run the installer from an interactive terminal."
  else
    if [ "$secret" = "1" ]; then
      read -rsp "    $prompt: " value < "$TTY_DEV"; echo
      [ -n "$value" ] || die "$var cannot be empty"
      value="$(printf '%s' "$value" | tr -d '[:space:]')"
    else
      read -rp  "    $prompt: " value < "$TTY_DEV"
      [ -n "$value" ] || die "$var cannot be empty"
      # Trim stray whitespace/CR pasted by terminal clients.
      value="$(printf '%s' "$value" | tr -d '[:space:]')"
    fi
  fi
  printf -v "$var" '%s' "$value"
  export "${var?}"
}

if [ ! -f "$ENV_FILE" ]; then
  say "Configuring .env"
  cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
  dim "Get API_ID / API_HASH from https://my.telegram.org → API development tools"
  dim "Get the bot token from @BotFather. Owner id from @userinfobot."
  echo
  ask SENTINEL_API_ID     "Telegram API_ID (digits)"
  ask SENTINEL_API_HASH   "Telegram API_HASH" 1
  ask CONTROL_BOT_TOKEN   "Control bot token (from @BotFather)" 1
  ask SENTINEL_OWNER_ID   "Your Telegram user id (owner/admin)"
else
  say ".env already exists — keeping existing configuration"
fi

# Write the collected values into .env (replace or append), never echoing secrets.
write_env() {
  local var="$1" val="$2"
  [ -n "${val:-}" ] || return 0
  if grep -q "^${var}=" "$ENV_FILE"; then
    # Use python to avoid sed escaping issues with special chars in tokens/hashes.
    "$INSTALL_DIR/.venv/bin/python" - "$ENV_FILE" "$var" "$val" <<'PY'
import sys, pathlib
path, var, val = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
lines = p.read_text().splitlines()
out, done = [], False
for line in lines:
    if line.startswith(var + "="):
        out.append(f"{var}={val}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{var}={val}")
p.write_text("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "$var" "$val" >> "$ENV_FILE"
  fi
}

write_env SENTINEL_API_ID   "${SENTINEL_API_ID:-}"
write_env SENTINEL_API_HASH "${SENTINEL_API_HASH:-}"
write_env CONTROL_BOT_TOKEN "${CONTROL_BOT_TOKEN:-}"
write_env SENTINEL_OWNER_ID "${SENTINEL_OWNER_ID:-}"

mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/data"
chmod 600 "$ENV_FILE"
chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"

# ----------------------------------------------------------------- systemd unit
say "Installing systemd service"
sed -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|/opt/sentinel|$INSTALL_DIR|g" \
    -e "s|^Description=.*|Description=Sentinel Platform (multi-user Telegram automation control plane)|" \
    "$INSTALL_DIR/deploy/sentinel.service" > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

# -------------------------------------------------------------------- verify
say "Waiting for the platform to boot"
BOOTED=0
for _ in $(seq 1 30); do
  sleep 2
  systemctl is-active --quiet "$SERVICE_NAME" || break
  if journalctl -u "$SERVICE_NAME" --no-pager -n 120 2>/dev/null | grep -q "Platform Ready"; then
    BOOTED=1; break
  fi
done

# A boot stamp alone is not proof of health: confirm the process is still alive
# a moment later and that the log has no Python traceback (bad token/API hash).
CRASHED=0
if [ "$BOOTED" = "1" ]; then
  sleep 8
  systemctl is-active --quiet "$SERVICE_NAME" || CRASHED=1
  if journalctl -u "$SERVICE_NAME" --no-pager -n 120 2>/dev/null | grep -qE "Traceback \(most recent call last\)|AuthKeyUnregistered|ApiIdInvalidError|RuntimeError:"; then
    CRASHED=1
  fi
fi

echo
if [ "$BOOTED" = "1" ] && [ "$CRASHED" = "0" ]; then
  printf '%s  Sentinel is live.%s\n\n' "$C_OK$C_WARN" "$C_OFF"
else
  warn "Installed, but the platform is not healthy yet — see the log tail below."
  echo
fi

OWNER="$(sed -n 's/^SENTINEL_OWNER_ID=//p' "$ENV_FILE" | head -1)"
cat <<EOF
  Next steps
  ──────────
  1. Open Telegram and DM your control bot.
  2. Send /start
  3. Register your selfbot: phone → OTP → 2FA (if set)
  4. Owner id $OWNER gets the admin panel automatically.

  Manage it
  ─────────
  systemctl status $SERVICE_NAME        # is it running?
  journalctl -u $SERVICE_NAME -f        # live logs
  systemctl restart $SERVICE_NAME       # restart
  nano $INSTALL_DIR/.env                # edit config, then restart

  Files
  ─────
  code   $INSTALL_DIR
  config $INSTALL_DIR/.env
  data   $INSTALL_DIR/data   (central.db + per-user sessions/DBs)
EOF

if [ "$BOOTED" != "1" ] || [ "$CRASHED" != "0" ]; then
  echo
  warn "Last 25 log lines:"
  journalctl -u "$SERVICE_NAME" --no-pager -n 25 2>/dev/null | sed 's/^/    /' || true
  exit 1
fi
