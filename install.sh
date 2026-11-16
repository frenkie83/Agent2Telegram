#!/usr/bin/env bash
# Agent2Telegram one-command installer.
# Usage:  curl -fsSL <raw-url>/install.sh | bash      (or run it from a clone)
# It checks Python, installs the package for the current user, and launches setup.
set -euo pipefail

# Recover the working directory if it was deleted (e.g. you just uninstalled while sitting in
# the source clone) — otherwise git/curl fail with "cannot access parent directories: getcwd".
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

REPO="https://github.com/petrludwig-collab/Agent2Telegram.git"
NEED_PY_MAJOR=3
NEED_PY_MINOR=10

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Pick where to drop the launcher. Prefer a directory ALREADY on PATH and writable — then the
# command works in the *current* terminal immediately (a piped installer can't change the parent
# shell's PATH, so installing into a dir it already searches is the only way to "just work" now).
# Fall back to ~/.local/bin (added to PATH via the rc files for future shells).
pick_bindir() {
  oldifs="$IFS"; IFS=:
  for d in $PATH; do
    case "$d" in
      "$HOME"/*) if [ -d "$d" ] && [ -w "$d" ]; then IFS="$oldifs"; printf '%s' "$d"; return; fi ;;
    esac
  done
  IFS="$oldifs"; printf '%s' "$HOME/.local/bin"
}

# 0) Preflight: collect EVERYTHING missing up front and give one install command, instead of
#    letting the user discover python, then tmux, then git one failed run at a time.
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
command -v tmux    >/dev/null 2>&1 || need="$need tmux"      # attach mode drives a tmux session
# git is only needed if we still have to CLONE (i.e. we're not already inside the project).
if ! { [ -f pyproject.toml ] && grep -q agent2telegram pyproject.toml 2>/dev/null; }; then
  command -v git >/dev/null 2>&1 || need="$need git"
fi
if [ -n "$need" ]; then
  need="${need# }"
  if command -v apt-get >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  sudo apt-get update && sudo apt-get install -y $need"
  elif command -v dnf >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  sudo dnf install -y $need"
  elif command -v brew >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  brew install $need"
  else
    err "Missing: $need — install these with your package manager, then re-run."
  fi
fi

# 1) Python version check (presence is guaranteed by the preflight above)
PY="$(command -v python3)"
"$PY" - <<'PYEOF' || err "Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ required."
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PYEOF
say "Using $("$PY" --version)"

# 2) Get the code (clone if we're not already inside it)
if [ -f "pyproject.toml" ] && grep -q "agent2telegram" pyproject.toml 2>/dev/null; then
  SRC="$(pwd)"
  say "Installing from current directory"
else
  command -v git >/dev/null || err "git not found (needed to fetch the project)."
  SRC="${HOME}/.agent2telegram-src"
  if [ -d "$SRC/.git" ]; then say "Updating $SRC"; git -C "$SRC" pull --ff-only
  else say "Cloning into $SRC"; git clone --depth 1 "$REPO" "$SRC"; fi
fi

# 3) Make `agent2telegram` a real command. pip is OPTIONAL (the core is pure standard library):
#    if pip is there we install the package too (so hooks can `python3 -m agent2telegram…`), but
#    EITHER WAY we drop our own launcher into a directory on PATH so the command works right after
#    install — no PYTHONPATH to remember, and (when a PATH dir is writable) no new shell needed.
if "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
  say "Installing the package"
  "$PY" -m pip install --user --upgrade "$SRC" >/dev/null 2>&1 \
    || "$PY" -m pip install --user --break-system-packages --upgrade "$SRC" >/dev/null 2>&1 || true
fi

BIND="$(pick_bindir)"
mkdir -p "$BIND"
printf '#!/bin/sh\nexec env PYTHONPATH="%s" "%s" -m agent2telegram "$@"\n' "$SRC" "$PY" > "$BIND/agent2telegram"
chmod +x "$BIND/agent2telegram"
RUN=("$BIND/agent2telegram"); HOW="agent2telegram"
case ":$PATH:" in
  *":$BIND:"*)
    say "Installed launcher in $BIND (already on PATH) — 'agent2telegram' works now." ;;
  *)
    say "Installed launcher in $BIND — adding it to PATH for new shells."
    for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
      [ -e "$rc" ] || continue
      grep -qs "$BIND" "$rc" || echo "export PATH=\"$BIND:\$PATH\"" >> "$rc"
    done
    # ~/.bashrc may not exist on a minimal server — create it so login shells pick it up.
    grep -qs "$BIND" "$HOME/.bashrc" 2>/dev/null || echo "export PATH=\"$BIND:\$PATH\"" >> "$HOME/.bashrc"
    export PATH="$BIND:$PATH"
    say "For THIS terminal, run:  export PATH=\"$BIND:\$PATH\"   (new terminals get it automatically)" ;;
esac

# 4) Launch the setup wizard.
# When invoked as `curl … | bash`, this script's stdin is the pipe, not your keyboard,
# so the interactive wizard must read from the controlling terminal (/dev/tty).
say "Run it later with:  $HOW run"
if [ -e /dev/tty ] && (: </dev/tty) 2>/dev/null; then
  say "Starting setup…"
  exec "${RUN[@]}" setup </dev/tty
else
  say "Installed. Finish setup with:"
  echo "    $HOW setup"
fi
