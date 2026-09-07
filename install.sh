#!/usr/bin/env bash
#
# install.sh - one-command, reviewable installer for Meetas (Linux).
#
# Installs only what is actually missing and shows what it will do first:
#   1. system packages : ffmpeg + PortAudio   (prints them, asks before using sudo)
#   2. uv              : user-level, no sudo  (provisions Python 3.12 automatically)
#   3. Python deps     : uv sync (locked, base only - no dev/diar extras)
#   4. web UI          : uses the prebuilt ui/dist (no Node/npm needed)
#   5. init            : storage + database
#   6. (optional)      : --with-models -> download the live ASR model
#   6b. (optional)     : --with-ollama -> install + start Ollama (AI runtime)
#   7. start           : daemon (skip with --no-start), then print the URL
#
set -euo pipefail

# --------------------------------------------------------------- flags
WITH_MODELS=0
WITH_OLLAMA=0
NO_START=0

usage() {
  cat <<'EOF'
install.sh - Meetas installer (Linux)

Usage: ./install.sh [options]

Options:
  --with-models   Also download the live ASR model after init (network required)
  --with-ollama   Also install Ollama (the AI-model runtime) and start it
                  (network required; the UI setup assistant continues after)
  --no-start      Install and initialize only; do not start the service
  -h, --help      Show this help and exit

By default no models are downloaded (they can be fetched on demand from the UI).
EOF
}

for arg in "$@"; do
  case "$arg" in
    --with-models)  WITH_MODELS=1 ;;
    --with-ollama)  WITH_OLLAMA=1 ;;
    --no-start)    NO_START=1 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

# --------------------------------------------------------------- helpers
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; O=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; O=""
fi
step() { printf '%s\n' "${B}==> $*${O}"; }
ok()   { printf '%s\n' "${G}${*}${O}"; }
warn() { printf '%s\n' "${Y}${*}${O}"; }
err()  { printf '%s\n' "${R}${*}${O}" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "${B}Meetas - Installation${O}"
echo "Target: ${ROOT}"
echo

# --------------------------------------------------------------- 1) system packages
step "Checking system requirements (ffmpeg, PortAudio) ..."
PKG=""
if   have apt-get; then PKG="apt"
elif have dnf;     then PKG="dnf"
elif have pacman;  then PKG="pacman"
fi

need_ffmpeg=0;  have ffmpeg || need_ffmpeg=1
need_portaudio=0
# PortAudio is a system library on Linux (apt/dnf/pacman). On macOS/Windows it is
# bundled in the sounddevice wheel, so it is not required here (Linux focus for v1).
case "$PKG" in
  apt|dnf|pacman)
    ldconfig -p 2>/dev/null | grep -qi portaudio || need_portaudio=1
    ;;
esac

PKGS=()
if (( need_ffmpeg )); then
  case "$PKG" in
    apt) PKGS+=(ffmpeg) ;;
    dnf|pacman) PKGS+=(ffmpeg) ;;
  esac
fi
if (( need_portaudio )); then
  case "$PKG" in
    apt) PKGS+=(libportaudio2) ;;
    dnf|pacman) PKGS+=(portaudio) ;;
  esac
fi

if [[ -z "$PKG" ]] && (( need_ffmpeg || need_portaudio )); then
  warn "No supported package manager (apt/dnf/pacman) found."
  echo "     Install manually:  ffmpeg   (and PortAudio, e.g. 'sudo apt install libportaudio2')"
  echo "     The Python installation continues; capture needs ffmpeg + PortAudio."
elif (( ${#PKGS[@]} > 0 )); then
  printf '  Will install (via %s): %s\n' "$PKG" "${PKGS[*]}"
  if [[ -t 0 ]]; then
    read -r -p "  Install now? [y/N] " ans
    [[ "${ans:-}" =~ ^[yYjJ] ]] || { warn "Aborted - system packages were NOT installed."; exit 1; }
  else
    echo "  (no interactive terminal -> installing automatically)"
  fi
  case "$PKG" in
    apt)    sudo apt-get update -y && sudo apt-get install -y --no-install-recommends "${PKGS[@]}" ;;
    dnf)    sudo dnf install -y "${PKGS[@]}" ;;
    pacman) sudo pacman -Sy --noconfirm --needed "${PKGS[@]}" ;;
  esac
  ok "System packages installed."
else
  ok "System packages: ffmpeg + PortAudio present."
fi
echo

# --------------------------------------------------------------- 2) uv (Python 3.12)
step "Checking uv (provides Python 3.12) ..."
if ! have uv; then
  echo "  uv not found -> installing uv (user-level, no sudo) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  have uv || { err "uv installation failed. Manually: https://docs.astral.sh/uv/"; exit 1; }
fi
ok "uv: $(uv --version 2>/dev/null || echo 'available')"
echo

# --------------------------------------------------------------- 3) Python deps (base)
step "Installing Python dependencies (uv sync, base only) ..."
export UV_PYTHON_DOWNLOADS=automatic   # non-interactive managed CPython 3.12 if needed
uv sync
ok "Python dependencies installed (base)."
echo

# --------------------------------------------------------------- 4) web UI (prebuilt)
step "Checking web UI (prebuilt in ui/dist) ..."
if [[ -f "$ROOT/ui/dist/index.html" ]]; then
  ok "Web UI present (ui/dist)."
elif have npm; then
  warn "ui/dist missing -> building the UI (npm) ..."
  ( cd ui && npm ci && npm run build )
  [[ -f "$ROOT/ui/dist/index.html" ]] || { err "UI build failed."; exit 1; }
  ok "Web UI built."
else
  err "ui/dist/index.html is missing and there is no Node/npm to build it."
  echo "     Please clone the full repository (ui/dist is included)."
  exit 1
fi
echo

# --------------------------------------------------------------- 5) init
step "Initializing storage + database ..."
uv run meeting-core init
echo

# --------------------------------------------------------------- 6) optional models
if (( WITH_MODELS )); then
  step "Downloading the live ASR model (requires network) ..."
  if uv run meeting-core download-model --confirm; then
    ok "ASR model ready."
  else
    warn "Model download failed. Retry:  uv run meeting-core download-model --confirm"
  fi
  echo
fi

# --------------------------------------------------------------- 6b) optional Ollama (AI runtime)
if (( WITH_OLLAMA )); then
  if have ollama; then
    ok "Ollama already installed."
  else
    step "Installing Ollama (network required) ..."
    # The official Ollama install script (v0.12+) needs zstd to extract the
    # bundle; Ubuntu/Debian do not ship it by default.
    if ! have zstd; then
      case "$PKG" in
        apt)    sudo apt-get install -y --no-install-recommends zstd ;;
        dnf)    sudo dnf install -y zstd ;;
        pacman) sudo pacman -Sy --noconfirm --needed zstd ;;
        *)      warn "zstd not found - the Ollama install script needs it (e.g. 'sudo apt-get install zstd')." ;;
      esac
    fi
    if [[ "$PKG" == "pacman" ]]; then
      # Arch: community package (the setup wizard gives the same hint in the UI)
      sudo pacman -Sy --noconfirm --needed ollama || warn "Ollama install failed. Retry:  sudo pacman -S ollama"
    else
      curl -fsSL https://ollama.com/install.sh | sh || warn "Ollama install failed. Retry:  curl -fsSL https://ollama.com/install.sh | sh"
    fi
  fi
  if have ollama; then
    # The package/script may not start the service; ensure it is up.
    if ! curl -fsS --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      step "Starting Ollama service ..."
      ( systemctl start ollama 2>/dev/null || sudo systemctl start ollama 2>/dev/null || nohup ollama serve >/dev/null 2>&1 & ) || true
      sleep 2
    fi
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      ok "Ollama is running. Models are fetched on demand by the UI setup assistant."
    else
      warn "Ollama is installed but not reachable yet - the UI setup assistant will guide you."
    fi
  else
    warn "Ollama could not be installed automatically. The UI setup assistant shows the command to run manually."
  fi
  echo
fi

# --------------------------------------------------------------- 7) start
if (( NO_START )); then
  ok "Installation + initialization complete (service not started)."
  echo "  Start with:   uv run meeting-core daemon"
else
  step "Starting the service (daemon) ..."
  if uv run meeting-core daemon; then
    sleep 1
    ok "Service started."
  else
    warn "Service start not confirmed. Manually:  uv run meeting-core daemon"
  fi
  echo
fi

ok "Ready. Open in your browser:  http://127.0.0.1:8765/"
echo "  Stop:             uv run meeting-core stop"
