#!/bin/bash
# PlumoAI Self-Hosted - macOS installer
# Prepares a Mac for PlumoAI, then hands off to install.sh (the cross-platform installer).
# Keep behavior aligned with install.ps1 (Windows) and install.sh (Linux / macOS / WSL).
#
# Usage:
#   ./install-macos.sh              # check prerequisites, then install and start
#   ./install-macos.sh --check      # check prerequisites only, change nothing
#   ./install-macos.sh --fresh      # pass --fresh through to install.sh
#   ./install-macos.sh --no-open    # do not open the browser at the end
#
# Written for the bash 3.2 that ships with macOS, so it avoids bash 4+ syntax
# and GNU-only flags (sed -i, readlink -f, base64 -w).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_URL="https://github.com/PlumoAI/plumoai.git"
MIN_MACOS_MAJOR=12          # Docker Desktop requires macOS 12 (Monterey) or newer
DOCKER_APP="/Applications/Docker.app"

CHECK_ONLY=false
OPEN_BROWSER=true
PASSTHRU=""

for arg in "$@"; do
  case "$arg" in
    --check)      CHECK_ONLY=true ;;
    --no-open)    OPEN_BROWSER=false ;;
    --fresh)      PASSTHRU="$PASSTHRU --fresh" ;;
    --no-backup)  PASSTHRU="$PASSTHRU --no-backup" ;;
    -h|--help)
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
      exit 0 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Run './install-macos.sh --help' for usage." >&2
      exit 1 ;;
  esac
done

# ---------- output helpers ----------

if [ -t 1 ]; then
  C_DIM='\033[0;90m'; C_RED='\033[0;31m'; C_GREEN='\033[0;32m'
  C_YELLOW='\033[0;33m'; C_MAGENTA='\033[0;35m'; C_CYAN='\033[0;96m'; C_OFF='\033[0m'
else
  C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_MAGENTA=''; C_CYAN=''; C_OFF=''
fi

ok()   { printf "  %sok%s    %s\n"   "$C_GREEN"  "$C_OFF" "$1"; }
info() { printf "  %s..%s    %s\n"   "$C_DIM"    "$C_OFF" "$1"; }
warn() { printf "  %swarn%s  %s\n"   "$C_YELLOW" "$C_OFF" "$1"; }
fail() { printf "  %sfail%s  %s\n"   "$C_RED"    "$C_OFF" "$1" >&2; }

banner() {
  echo ""
  printf "  %sPlumo%sAi%s\n" "$C_MAGENTA" "$C_CYAN" "$C_OFF"
  printf "  %sSelf-Hosted - macOS installer%s\n" "$C_DIM" "$C_OFF"
  echo ""
}

die() {
  echo ""
  fail "$1"
  [ -n "$2" ] && printf "        %s\n" "$2" >&2
  echo ""
  exit 1
}

# ---------- spinner ----------
# Braille frames for the animated wait indicator.
SPIN_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

# spin_wait <timeout_secs> <message> <check-cmd...>
# Animates a spinner while polling <check-cmd> until it succeeds, or times out.
# Returns 0 on success, 1 on timeout. Leaves no residual line: the caller prints
# the result. Falls back to plain dots when stdout is not a terminal (e.g. a log).
spin_wait() {
  local timeout="$1" msg="$2"; shift 2
  local i=0 secs=0 n="${#SPIN_FRAMES[@]}"

  if [ ! -t 1 ]; then
    printf "  %s..%s    %s" "$C_DIM" "$C_OFF" "$msg"
    while [ "$secs" -lt "$timeout" ]; do
      "$@" >/dev/null 2>&1 && { printf "\n"; return 0; }
      printf "."; sleep 2; secs=$((secs + 2))
    done
    printf "\n"; return 1
  fi

  printf '\033[?25l'  # hide cursor
  while [ "$secs" -lt "$timeout" ]; do
    # Poll roughly once a second (every 8 frames at ~0.12s each).
    if [ $((i % 8)) -eq 0 ] && "$@" >/dev/null 2>&1; then
      printf '\r\033[K\033[?25h'   # clear line, restore cursor
      return 0
    fi
    printf '\r  %s%s%s    %s' "$C_CYAN" "${SPIN_FRAMES[$((i % n))]}" "$C_OFF" "$msg"
    sleep 0.12
    i=$((i + 1))
    secs=$((i * 12 / 100))
  done
  printf '\r\033[K\033[?25h'       # clear line, restore cursor
  return 1
}

# ---------- checks ----------

check_macos() {
  if [ "$(uname -s)" != "Darwin" ]; then
    die "This installer is for macOS." "On Linux or WSL use ./install.sh, on Windows use install.ps1."
  fi

  local ver major
  ver="$(sw_vers -productVersion 2>/dev/null || echo "0")"
  major="$(echo "$ver" | cut -d. -f1)"

  if [ "$major" -lt "$MIN_MACOS_MAJOR" ] 2>/dev/null; then
    die "macOS $ver is not supported." \
        "Docker Desktop needs macOS $MIN_MACOS_MAJOR (Monterey) or newer."
  fi
  ok "macOS $ver ($(uname -m))"
}

check_homebrew() {
  if command -v brew >/dev/null 2>&1; then
    ok "Homebrew $(brew --version 2>/dev/null | head -1 | awk '{print $2}')"
    return 0
  fi
  warn "Homebrew not found (only needed to auto-install Docker Desktop)"
  return 1
}

check_git() {
  if ! command -v git >/dev/null 2>&1; then
    die "Git not found." \
        "Install the Command Line Tools with:  xcode-select --install"
  fi
  ok "git $(git --version | awk '{print $3}')"
}

docker_installed() {
  command -v docker >/dev/null 2>&1 || [ -d "$DOCKER_APP" ]
}

docker_running() {
  docker info >/dev/null 2>&1
}

install_docker() {
  echo ""
  info "Docker Desktop is required and was not found."

  if [ "$CHECK_ONLY" = true ]; then
    warn "check mode: would install Docker Desktop via Homebrew"
    return 1
  fi

  if ! command -v brew >/dev/null 2>&1; then
    die "Cannot install Docker Desktop automatically without Homebrew." \
        "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ then re-run."
  fi

  printf "  Install Docker Desktop now via Homebrew? [y/N] "
  read -r reply
  case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *) die "Docker Desktop is required to continue." \
           "Install it from https://www.docker.com/products/docker-desktop/ then re-run." ;;
  esac

  info "Installing Docker Desktop (this can take a few minutes)..."
  if ! brew install --cask docker; then
    die "Homebrew failed to install Docker Desktop." \
        "Install it manually from https://www.docker.com/products/docker-desktop/"
  fi
  ok "Docker Desktop installed"
}

start_docker() {
  if [ "$CHECK_ONLY" = true ]; then
    warn "check mode: would start Docker Desktop and wait for the daemon"
    return 1
  fi

  info "Starting Docker Desktop..."
  open -a Docker 2>/dev/null || die "Could not launch Docker Desktop." "Open it from Applications, then re-run."

  if spin_wait 120 "Waiting for the Docker daemon" docker info; then
    ok "Docker daemon is running"
    return 0
  fi

  die "Docker did not become ready within 120 seconds." \
      "Open Docker Desktop, finish any first-run setup, then re-run this script."
}

check_docker() {
  if ! docker_installed; then
    install_docker || return 1
  else
    ok "Docker Desktop found"
  fi

  if ! docker_running; then
    warn "Docker daemon is not running"
    start_docker || return 1
  else
    ok "Docker daemon is running"
  fi

  # install.sh needs one of these; check the same way it does
  if docker compose version >/dev/null 2>&1; then
    ok "Docker Compose v2 ($(docker compose version --short 2>/dev/null))"
  elif command -v docker-compose >/dev/null 2>&1; then
    ok "docker-compose (legacy) found"
  else
    die "Docker Compose not found." \
        "Docker Desktop normally bundles Compose v2. Update Docker Desktop and re-run."
  fi
}

# ---------- repo ----------

resolve_repo() {
  # Already inside a checkout (install.sh sits next to this script)
  if [ -f "$SCRIPT_DIR/install.sh" ] && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    REPO_DIR="$SCRIPT_DIR"
    ok "Using existing checkout: $REPO_DIR"
    return 0
  fi

  REPO_DIR="$PWD/plumoai"

  if [ -d "$REPO_DIR/.git" ]; then
    ok "Found existing clone: $REPO_DIR"
    return 0
  fi

  if [ "$CHECK_ONLY" = true ]; then
    warn "check mode: would clone $REPO_URL into $REPO_DIR"
    return 1
  fi

  info "Cloning PlumoAI into $REPO_DIR ..."
  git clone "$REPO_URL" "$REPO_DIR" || die "git clone failed." "Check your network connection and try again."
  ok "Cloned"
}

# ---------- run ----------

run_installer() {
  cd "$REPO_DIR"

  if [ ! -f install.sh ]; then
    die "install.sh not found in $REPO_DIR." "The checkout looks incomplete."
  fi

  chmod +x install.sh
  echo ""
  info "Handing off to install.sh ..."
  echo ""

  # shellcheck disable=SC2086
  ./install.sh $PASSTHRU
}

app_url() {
  # Read the URL install.sh would report, from the .env it created
  local run_mode port domain
  [ -f "$REPO_DIR/.env" ] || return 1

  run_mode="$(grep -E '^RUN_MODE=' "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  port="$(grep -E '^LOCALHOST_PORT=' "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
  domain="$(grep -E '^DOMAIN_NAME=' "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')"

  if [ "$run_mode" = "domain" ] && [ -n "$domain" ]; then
    echo "https://$domain"
  else
    echo "http://localhost:${port:-7861}"
  fi
}

# ---------- main ----------

banner

if [ "$CHECK_ONLY" = true ]; then
  printf "  %scheck mode - nothing will be installed or changed%s\n\n" "$C_DIM" "$C_OFF"
fi

echo "Checking prerequisites"
check_macos
check_git
check_homebrew || true
check_docker || CHECK_INCOMPLETE=true

echo ""
echo "Repository"
resolve_repo || CHECK_INCOMPLETE=true

if [ "$CHECK_ONLY" = true ]; then
  echo ""
  if [ "${CHECK_INCOMPLETE:-false}" = true ]; then
    printf "  %sSome steps would run on a real install (shown above).%s\n" "$C_YELLOW" "$C_OFF"
  else
    printf "  %sThis Mac is ready. Run ./install-macos.sh to install.%s\n" "$C_GREEN" "$C_OFF"
  fi
  echo ""
  exit 0
fi

run_installer

URL="$(app_url || true)"
if [ -n "$URL" ]; then
  echo ""
  ok "PlumoAI is running at $URL"
  if [ "$OPEN_BROWSER" = true ]; then
    info "Opening $URL ..."
    open "$URL" 2>/dev/null || true
  fi
fi
echo ""
