#!/usr/bin/env bash
#
# install.sh - one-command, reviewable installer for the Meeting Assistant (Linux).
#
# Installs only what is actually missing and shows what it will do first:
#   1. system packages : ffmpeg + PortAudio   (prints them, asks before using sudo)
#   2. uv              : user-level, no sudo  (provisions Python 3.12 automatically)
#   3. Python deps     : uv sync (locked, base only - no dev/diar extras)
#   4. web UI          : uses the prebuilt ui/dist (no Node/npm needed)
#   5. init            : storage + database
#   6. (optional)      : --with-models -> download the live ASR model
#   7. start           : daemon (skip with --no-start), then print the URL
#
set -euo pipefail

# --------------------------------------------------------------- flags
WITH_MODELS=0
NO_START=0

usage() {
  cat <<'EOF'
install.sh - Meeting Assistant installer (Linux)

Usage: ./install.sh [options]

Options:
  --with-models   Also download the live ASR model after init (network required)
  --no-start      Install and initialize only; do not start the service
  -h, --help      Show this help and exit

By default no models are downloaded (they can be fetched on demand from the UI).
EOF
}

for arg in "$@"; do
  case "$arg" in
    --with-models) WITH_MODELS=1 ;;
    --no-start)    NO_START=1 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unbekanntes Argument: $arg" >&2; usage; exit 2 ;;
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

echo "${B}Meeting Assistant - Installation${O}"
echo "Ziel: ${ROOT}"
echo

# --------------------------------------------------------------- 1) system packages
step "Prüfe System-Voraussetzungen (ffmpeg, PortAudio) ..."
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
  warn "Kein unterstützter Paketmanager (apt/dnf/pacman) gefunden."
  echo "     Manuell installieren:  ffmpeg   (und PortAudio, z.B. 'sudo apt install libportaudio2')"
  echo "     Die Python-Installation wird fortgesetzt; Aufnahme braucht ffmpeg + PortAudio."
elif (( ${#PKGS[@]} > 0 )); then
  printf '  Es werden installiert (via %s): %s\n' "$PKG" "${PKGS[*]}"
  if [[ -t 0 ]]; then
    read -r -p "  Jetzt installieren? [j/N] " ans
    [[ "${ans:-}" =~ ^[jJyY] ]] || { warn "Abgebrochen - Systempakete wurden NICHT installiert."; exit 1; }
  else
    echo "  (kein interaktives Terminal -> installiere automatisch)"
  fi
  case "$PKG" in
    apt)    sudo apt-get update -y && sudo apt-get install -y --no-install-recommends "${PKGS[@]}" ;;
    dnf)    sudo dnf install -y "${PKGS[@]}" ;;
    pacman) sudo pacman -Sy --noconfirm --needed "${PKGS[@]}" ;;
  esac
  ok "Systempakete installiert."
else
  ok "Systempakete: ffmpeg + PortAudio vorhanden."
fi
echo

# --------------------------------------------------------------- 2) uv (Python 3.12)
step "Prüfe uv (bereit stellt Python 3.12) ..."
if ! have uv; then
  echo "  uv nicht gefunden -> installiere uv (user-level, ohne sudo) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  have uv || { err "uv-Installation fehlgeschlagen. Manuell: https://docs.astral.sh/uv/"; exit 1; }
fi
ok "uv: $(uv --version 2>/dev/null || echo 'verfügbar')"
echo

# --------------------------------------------------------------- 3) Python deps (base)
step "Installiere Python-Abhängigkeiten (uv sync, nur Basis) ..."
export UV_PYTHON_DOWNLOADS=automatic   # non-interactive managed CPython 3.12 if needed
uv sync
ok "Python-Abhängigkeiten installiert (Basis)."
echo

# --------------------------------------------------------------- 4) web UI (prebuilt)
step "Prüfe Web-UI (vorgebaut in ui/dist) ..."
if [[ -f "$ROOT/ui/dist/index.html" ]]; then
  ok "Web-UI vorhanden (ui/dist)."
elif have npm; then
  warn "ui/dist fehlt -> baue die UI (npm) ..."
  ( cd ui && npm ci && npm run build )
  [[ -f "$ROOT/ui/dist/index.html" ]] || { err "UI-Build fehlgeschlagen."; exit 1; }
  ok "Web-UI gebaut."
else
  err "ui/dist/index.html fehlt und es ist kein Node/npm zum Bauen vorhanden."
  echo "     Bitte das Repository vollständig klonen (ui/dist wird mitgeliefert)."
  exit 1
fi
echo

# --------------------------------------------------------------- 5) init
step "Initialisiere Speicher + Datenbank ..."
uv run meeting-core init
echo

# --------------------------------------------------------------- 6) optional models
if (( WITH_MODELS )); then
  step "Lade Live-ASR-Modell herunter (benötigt Netzwerk) ..."
  if uv run meeting-core download-model --confirm; then
    ok "ASR-Modell bereit."
  else
    warn "Modell-Download fehlgeschlagen. Nachholen:  uv run meeting-core download-model --confirm"
  fi
  echo
fi

# --------------------------------------------------------------- 7) start
if (( NO_START )); then
  ok "Installation + Initialisierung abgeschlossen (Dienst nicht gestartet)."
  echo "  Starten mit:   uv run meeting-core daemon"
else
  step "Starte den Dienst (daemon) ..."
  if uv run meeting-core daemon; then
    sleep 1
    ok "Dienst gestartet."
  else
    warn "Dienst-Start nicht bestätigt. Manuell:  uv run meeting-core daemon"
  fi
  echo
fi

ok "Bereit. Öffne im Browser:  http://127.0.0.1:8765/"
echo "  Stopp:            uv run meeting-core stop"
