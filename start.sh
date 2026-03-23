#!/usr/bin/env bash
set -euo pipefail

# ==========================================
# Meeting Transcriber -- Startskript
# ==========================================
# Startet Ollama, aktiviert das venv und startet main.py
#
# Verwendung:
#   chmod +x start.sh
#   ./start.sh
# ==========================================

# --- Konfiguration ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"

# SSD_PATH aus .env lesen (gleiche Logik wie main.py)
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    SSD_PATH="$(grep -E '^SSD_PATH=' "$SCRIPT_DIR/.env" | cut -d'=' -f2-)"
fi
SSD_PATH="${SSD_PATH:-/Volumes/ExtSSD/WhisperSystem}"

VENV_DIR="$SSD_PATH/venv"
MAIN_PY="$SSD_PATH/main.py"

# --- Pruefungen ---
if [[ ! -d "$SSD_PATH" ]]; then
    echo "❌ SSD-Pfad nicht gefunden: $SSD_PATH"
    echo "   Ist die externe Festplatte angeschlossen?"
    exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "❌ Virtual Environment nicht gefunden: $VENV_DIR"
    echo "   Zuerst ./install.sh ausfuehren."
    exit 1
fi

if [[ ! -f "$MAIN_PY" ]]; then
    echo "❌ main.py nicht gefunden: $MAIN_PY"
    exit 1
fi

# --- Ollama starten (falls nicht bereits aktiv) ---
if ! pgrep -q -x "Ollama"; then
    echo "🔄 Starte Ollama..."
    open -a Ollama
    # Warten bis Ollama-API erreichbar ist
    for i in {1..30}; do
        if curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
            echo "✅ Ollama laeuft."
            break
        fi
        sleep 1
    done
    if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "❌ Ollama konnte nicht gestartet werden."
        exit 1
    fi
else
    echo "✅ Ollama laeuft bereits."
fi

# --- venv aktivieren und main.py starten ---
echo "🔄 Aktiviere venv und starte Meeting Transcriber..."
source "$VENV_DIR/bin/activate"
python "$MAIN_PY"
