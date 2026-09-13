#!/bin/bash
# PlumoAI Installer for macOS - double-clickable bootstrapper.
#
# This is the macOS counterpart to PlumoAISetup.exe on Windows: a Mac user
# downloads this one file, double-clicks it, and it installs PlumoAI.
#
# It fetches the PlumoAI repository (or uses a checkout it is sitting inside),
# then hands off to install-macos.sh, which checks prerequisites, installs
# Docker Desktop if needed, and starts the stack.
#
# Written for the bash 3.2 that ships with macOS.

# Keep the Terminal window open on exit so double-click users see the result.
trap 'printf "\n  Press Return to close this window."; read -r _' EXIT

REPO_URL="https://github.com/PlumoAI/plumoai.git"
# Where to clone when this file is run on its own (override for testing).
INSTALL_DIR="${PLUMOAI_INSTALL_DIR:-$HOME/PlumoAI}"

# ---------- output ----------
if [ -t 1 ]; then
  DIM='\033[0;90m'; RED='\033[0;31m'; GRN='\033[0;32m'
  MAG='\033[0;35m'; CYN='\033[0;96m'; OFF='\033[0m'
else
  DIM=''; RED=''; GRN=''; MAG=''; CYN=''; OFF=''
fi

printf "\n  %sPlumo%sAi%s\n" "$MAG" "$CYN" "$OFF"
printf "  %sInstaller for macOS%s\n\n" "$DIM" "$OFF"

die() {
  printf "\n  %s%s%s\n" "$RED" "$1" "$OFF" >&2
  [ -n "$2" ] && printf "        %s\n" "$2" >&2
  exit 1
}

# ---------- locate install-macos.sh ----------
# Case 1: this .command was distributed inside a repo checkout, next to the
# installer script. Use that checkout directly.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SELF_DIR/install-macos.sh" ] && [ -f "$SELF_DIR/install.sh" ]; then
  REPO_DIR="$SELF_DIR"
  printf "  %s..%s    Using installer next to this file: %s\n" "$DIM" "$OFF" "$REPO_DIR"
else
  # Case 2: standalone download. We need git to fetch the repo.
  if ! command -v git >/dev/null 2>&1; then
    printf "  %s..%s    Git is required and was not found. Requesting the Command Line Tools...\n" "$DIM" "$OFF"
    xcode-select --install 2>/dev/null || true
    die "Git (Xcode Command Line Tools) needs to be installed first." \
        "A system dialog should have opened. Finish that install, then double-click this file again."
  fi

  if [ -d "$INSTALL_DIR/.git" ]; then
    printf "  %s..%s    Updating existing PlumoAI at %s ...\n" "$DIM" "$OFF" "$INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || \
      printf "  %swarn%s  could not fast-forward; using the existing copy as-is\n" "$RED" "$OFF"
  else
    printf "  %s..%s    Downloading PlumoAI into %s ...\n" "$DIM" "$OFF" "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR" || \
      die "Download failed." "Check your internet connection and try again."
  fi
  REPO_DIR="$INSTALL_DIR"
fi

# ---------- hand off ----------
cd "$REPO_DIR" || die "Could not enter $REPO_DIR."

# Runs a command, or just prints it when PLUMOAI_DRY_RUN is set (used by tests).
run_or_echo() {
  if [ -n "${PLUMOAI_DRY_RUN:-}" ]; then
    printf "  %s[dry-run]%s would run: %s\n" "$DIM" "$OFF" "$*"
    return 0
  fi
  "$@"
}

printf "  %sok%s    Ready. Starting the installer...\n\n" "$GRN" "$OFF"
printf "  %s----------------------------------------------------------%s\n\n" "$DIM" "$OFF"

if [ -f install-macos.sh ]; then
  # Preferred path: the full macOS installer (checks prerequisites, installs
  # Docker Desktop if needed, then runs install.sh).
  chmod +x install-macos.sh 2>/dev/null || true
  run_or_echo ./install-macos.sh "$@"
  status=$?
else
  # Fallback: this version of the repo predates install-macos.sh (e.g. it has
  # not been merged yet). Run install.sh directly, after making sure Docker is
  # up ourselves, since install.sh does not install Docker.
  printf "  %s..%s    install-macos.sh not present in this version; using install.sh directly.\n" "$DIM" "$OFF"

  if ! docker info >/dev/null 2>&1; then
    if [ -d /Applications/Docker.app ]; then
      printf "  %s..%s    Starting Docker Desktop...\n" "$DIM" "$OFF"
      open -a Docker 2>/dev/null || true
      w=0
      while [ "$w" -lt 120 ]; do
        docker info >/dev/null 2>&1 && break
        sleep 3; w=$((w + 3))
      done
    fi
  fi

  if ! docker info >/dev/null 2>&1; then
    die "Docker Desktop is required and is not running." \
        "Install it from https://www.docker.com/products/docker-desktop/ then double-click this file again."
  fi

  [ -f install.sh ] || die "install.sh is missing from the download." "The repository may be incomplete; try again."
  chmod +x install.sh 2>/dev/null || true
  run_or_echo ./install.sh "$@"
  status=$?
fi

printf "\n  %s----------------------------------------------------------%s\n" "$DIM" "$OFF"
if [ "$status" -eq 0 ]; then
  printf "  %sDone.%s PlumoAI is installed in %s\n" "$GRN" "$OFF" "$REPO_DIR"
else
  printf "  %sThe installer exited with an error (code %s).%s\n" "$RED" "$status" "$OFF"
  printf "        The output above shows what happened.\n"
fi

exit "$status"
