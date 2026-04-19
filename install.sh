#!/usr/bin/env bash
set -euo pipefail

# ==========================================
# Meeting Transcriber -- Installer
# ==========================================
# Automatische Installation aller Abhaengigkeiten
# Sicher mehrfach ausfuehrbar (idempotent)
#
# Verwendung:
#   chmod +x install.sh
#   ./install.sh                              # Fragt nach dem SSD-Pfad
#   ./install.sh /Volumes/ExtSSD/WhisperSystem  # Pfad als Argument
# ==========================================

# --- Farben ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# --- Konstanten ---
PYTHON_VERSION="3.11.9"
OLLAMA_MODEL="mistral-nemo"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
PYTHON_BIN="$PYENV_ROOT/versions/$PYTHON_VERSION/bin/python"
DEFAULT_SSD_PATH="$(dirname "$REPO_DIR")/WhisperSystem"

# --- Hilfsfunktionen ---
step_num=0
total_steps=12

step() {
    step_num=$((step_num + 1))
    echo ""
    echo -e "${BOLD}=== Schritt $step_num/$total_steps: $1 ===${RESET}"
}

info()    { echo -e "${BLUE}[INFO]${RESET} $1"; }
success() { echo -e "${GREEN}[OK]${RESET}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $1"; }
skip()    { echo -e "${DIM}[SKIP]${RESET} $1"; }

error() {
    echo -e "${RED}[FEHLER]${RESET} $1"
    exit 1
}

# ==========================================
# Schritt 1: Voraussetzungen pruefen
# ==========================================
step "Voraussetzungen pruefen"

# Apple Silicon?
ARCH=$(uname -m)
if [ "$ARCH" != "arm64" ]; then
    error "Apple Silicon (M1/M2/M3/M4) erforderlich. Erkannt: $ARCH"
fi
success "Apple Silicon ($ARCH)"

# macOS?
OS=$(uname -s)
if [ "$OS" != "Darwin" ]; then
    error "macOS erforderlich. Erkannt: $OS"
fi
success "macOS"

# Homebrew?
if ! command -v brew &>/dev/null; then
    error "Homebrew nicht gefunden. Installiere es zuerst: https://brew.sh"
fi
success "Homebrew"

# RAM pruefen
RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
RAM_GB=$((RAM_BYTES / 1073741824))
if [ "$RAM_GB" -lt 16 ]; then
    warn "Nur ${RAM_GB} GB RAM erkannt. 16 GB empfohlen fuer optimale Performance."
else
    success "${RAM_GB} GB RAM"
fi

# ==========================================
# Schritt 2: System-Abhaengigkeiten installieren
# ==========================================
step "System-Abhaengigkeiten installieren (Homebrew)"

# pyenv
if brew list pyenv &>/dev/null; then
    skip "pyenv bereits installiert"
else
    info "Installiere pyenv..."
    brew install pyenv
    success "pyenv installiert"
fi

# ffmpeg
if brew list ffmpeg &>/dev/null; then
    skip "ffmpeg bereits installiert"
else
    info "Installiere ffmpeg..."
    brew install ffmpeg
    success "ffmpeg installiert"
fi

# Ollama
if brew list --cask ollama &>/dev/null; then
    skip "Ollama bereits installiert"
else
    info "Installiere Ollama..."
    brew install --cask ollama
    success "Ollama installiert"
fi

# ==========================================
# Schritt 3: pyenv in ~/.zshrc eintragen
# ==========================================
step "pyenv in Shell konfigurieren"

PYENV_INIT_LINE='eval "$(pyenv init -)"'
ZSHRC="$HOME/.zshrc"

if [ ! -f "$ZSHRC" ]; then
    touch "$ZSHRC"
    info "~/.zshrc erstellt"
fi

if grep -qF 'pyenv init' "$ZSHRC"; then
    skip "pyenv init bereits in ~/.zshrc"
else
    echo "" >> "$ZSHRC"
    echo "# pyenv (Python Version Manager)" >> "$ZSHRC"
    echo "$PYENV_INIT_LINE" >> "$ZSHRC"
    success "pyenv init zu ~/.zshrc hinzugefuegt"
fi

# pyenv im aktuellen Script aktivieren
export PYENV_ROOT
export PATH="$PYENV_ROOT/shims:$PATH"
eval "$(pyenv init --path)" 2>/dev/null || true
eval "$(pyenv init -)" 2>/dev/null || true

# ==========================================
# Schritt 4: Python 3.11.9 installieren
# ==========================================
step "Python $PYTHON_VERSION installieren"

if [ -x "$PYTHON_BIN" ]; then
    skip "Python $PYTHON_VERSION bereits installiert"
else
    info "Installiere Python $PYTHON_VERSION (kann einige Minuten dauern)..."
    pyenv install "$PYTHON_VERSION"
    success "Python $PYTHON_VERSION installiert"
fi

# Verifizieren
PY_VER=$("$PYTHON_BIN" --version 2>&1)
success "$PY_VER verfuegbar unter $PYTHON_BIN"

# ==========================================
# Schritt 5: SSD-Pfad festlegen
# ==========================================
step "Projektordner festlegen"

if [ -n "${1:-}" ]; then
    SSD_PATH="$1"
    info "Pfad aus Argument: $SSD_PATH"
else
    echo -e "Wo soll das Projekt installiert werden?"
    echo -e "  Default: ${BOLD}$DEFAULT_SSD_PATH${RESET}"
    echo -n "  Pfad eingeben (oder Enter fuer Default): "
    read -r USER_PATH
    SSD_PATH="${USER_PATH:-$DEFAULT_SSD_PATH}"
fi

# Pruefen ob Elternverzeichnis existiert
PARENT_DIR=$(dirname "$SSD_PATH")
if [ ! -d "$PARENT_DIR" ]; then
    warn "Verzeichnis '$PARENT_DIR' existiert nicht."
    echo -n "  Trotzdem erstellen? [j/N]: "
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[jJyY]$ ]]; then
        error "Abgebrochen. Bitte SSD einstecken oder anderen Pfad waehlen."
    fi
fi

success "Projektpfad: $SSD_PATH"

# ==========================================
# Schritt 6: Projektordner erstellen
# ==========================================
step "Projektordner erstellen"

mkdir -p "$SSD_PATH"/{input,output,archive,temp,failed,models}
success "Ordnerstruktur erstellt"

# pyenv local Version setzen
pyenv local "$PYTHON_VERSION" 2>/dev/null || \
    (cd "$SSD_PATH" && pyenv local "$PYTHON_VERSION")
success ".python-version gesetzt"

# ==========================================
# Schritt 7: Virtual Environment erstellen
# ==========================================
step "Python Virtual Environment erstellen"

VENV_PYTHON="$SSD_PATH/venv/bin/python"
PIP="$SSD_PATH/venv/bin/pip"

if [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" --version &>/dev/null; then
    skip "venv bereits vorhanden und funktionsfaehig"
else
    info "Erstelle venv mit $PYTHON_BIN..."
    "$PYTHON_BIN" -m venv "$SSD_PATH/venv"
    success "venv erstellt"
fi

# pip aktualisieren
info "Aktualisiere pip..."
"$PIP" install --upgrade pip --quiet
success "pip aktualisiert"

# ==========================================
# Schritt 8: Python-Pakete installieren
# ==========================================
step "Python-Pakete installieren"

info "Installiere Basis-Pakete (mlx-whisper, watchdog, etc.)..."
"$PIP" install --quiet mlx-whisper python-dotenv watchdog requests

info "Installiere Konvertierungs-Pakete (transformers, torch)..."
info "(Dies kann einige Minuten dauern, ~2 GB Download)"
"$PIP" install --quiet transformers torch

success "Alle Basis-Pakete installiert"

# ==========================================
# Schritt 9: Optional: Sprechererkennung
# ==========================================
step "Sprechererkennung (optional)"

DIARIZATION_ENABLED="false"
HF_TOKEN_VALUE=""

echo -e "  Sprechererkennung installieren (pyannote.audio)?"
echo -e "  Erkennt WER was gesagt hat. Benoetigt HuggingFace Token."
echo -n "  Installieren? [j/N]: "
read -r INSTALL_DIARIZATION

if [[ "$INSTALL_DIARIZATION" =~ ^[jJyY]$ ]]; then
    info "Installiere pyannote.audio..."
    "$PIP" install --quiet pyannote.audio
    success "pyannote.audio installiert"
    DIARIZATION_ENABLED="true"

    echo ""
    echo -e "  HuggingFace Token eingeben (erstellen unter: https://huggingface.co/settings/tokens)"
    echo -e "  ${DIM}Nutzungsbedingungen akzeptieren:${RESET}"
    echo -e "  ${DIM}  https://huggingface.co/pyannote/speaker-diarization-3.1${RESET}"
    echo -e "  ${DIM}  https://huggingface.co/pyannote/segmentation-3.0${RESET}"
    echo -n "  HF Token (hf_...): "
    read -r HF_TOKEN_VALUE

    if [ -n "$HF_TOKEN_VALUE" ]; then
        success "HF Token gespeichert"
    else
        warn "Kein Token eingegeben. Sprechererkennung wird ohne Token nicht funktionieren."
        warn "Du kannst den Token spaeter in $SSD_PATH/.env nachtragen."
    fi
else
    skip "Sprechererkennung uebersprungen"
fi

# ==========================================
# Schritt 10: Projektdateien kopieren
# ==========================================
step "Projektdateien kopieren"

# main.py
if [ -f "$REPO_DIR/main.py" ]; then
    cp "$REPO_DIR/main.py" "$SSD_PATH/main.py"
    success "main.py kopiert"
else
    error "main.py nicht gefunden in $REPO_DIR"
fi

# .env.example
if [ -f "$REPO_DIR/.env.example" ]; then
    cp "$REPO_DIR/.env.example" "$SSD_PATH/.env.example"
    success ".env.example kopiert"
else
    warn ".env.example nicht gefunden -- uebersprungen"
fi

# .env erstellen (nur wenn noch nicht vorhanden)
if [ -f "$SSD_PATH/.env" ]; then
    skip ".env existiert bereits (wird nicht ueberschrieben)"
else
    if [ -f "$SSD_PATH/.env.example" ]; then
        sed \
            -e "s|^SSD_PATH=.*|SSD_PATH=$SSD_PATH|" \
            -e "s|^ENABLE_DIARIZATION=.*|ENABLE_DIARIZATION=$DIARIZATION_ENABLED|" \
            -e "s|^HF_TOKEN=.*|HF_TOKEN=$HF_TOKEN_VALUE|" \
            "$SSD_PATH/.env.example" > "$SSD_PATH/.env"
        success ".env erstellt mit SSD_PATH=$SSD_PATH"
    else
        # Fallback: .env manuell erstellen
        cat > "$SSD_PATH/.env" <<ENVEOF
SSD_PATH=$SSD_PATH
WHISPER_MODEL=auto
OLLAMA_MODEL=mistral-nemo
OLLAMA_URL=http://localhost:11434
ENABLE_DIARIZATION=$DIARIZATION_ENABLED
HF_TOKEN=$HF_TOKEN_VALUE
ENVEOF
        success ".env manuell erstellt"
    fi
fi

# ==========================================
# Schritt 11: Ollama starten und Modell laden
# ==========================================
step "Ollama starten und Modell laden"

# Pruefen ob Ollama laeuft
if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    skip "Ollama laeuft bereits"
else
    info "Starte Ollama..."
    open -a Ollama

    # Warten bis Ollama bereit ist (max 30 Sekunden)
    WAIT=0
    while ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
        if [ $WAIT -ge 30 ]; then
            warn "Ollama antwortet nicht nach 30 Sekunden."
            warn "Bitte Ollama manuell starten und dann ausfuehren: ollama pull $OLLAMA_MODEL"
            break
        fi
        sleep 2
        WAIT=$((WAIT + 2))
        echo -n "."
    done
    echo ""

    if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        success "Ollama gestartet"
    fi
fi

# Modell pullen
if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
    skip "$OLLAMA_MODEL bereits vorhanden"
else
    if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        info "Lade $OLLAMA_MODEL (~4 GB, kann einige Minuten dauern)..."
        ollama pull "$OLLAMA_MODEL"
        success "$OLLAMA_MODEL geladen"
    else
        warn "Ollama nicht erreichbar. Modell spaeter laden mit: ollama pull $OLLAMA_MODEL"
    fi
fi

# ==========================================
# Schritt 12: Zusammenfassung
# ==========================================
step "Fertig!"

echo ""
echo -e "${GREEN}${BOLD}================================================${RESET}"
echo -e "${GREEN}${BOLD}  Meeting Transcriber -- Installation komplett!${RESET}"
echo -e "${GREEN}${BOLD}================================================${RESET}"
echo ""
echo -e "  Projektordner:      ${BOLD}$SSD_PATH${RESET}"
echo -e "  Python:             $PY_VER"
echo -e "  Virtual Environment: $SSD_PATH/venv"
echo -e "  Ollama-Modell:      $OLLAMA_MODEL"
echo -e "  Sprechererkennung:  $DIARIZATION_ENABLED"
echo ""
echo -e "  ${BOLD}So startest du den Meeting Transcriber:${RESET}"
echo ""
echo -e "    cd \"$SSD_PATH\""
echo -e "    source venv/bin/activate"
echo -e "    python main.py"
echo ""
echo -e "  ${BOLD}Oder direkt (ohne venv zu aktivieren):${RESET}"
echo ""
echo -e "    \"$SSD_PATH/venv/bin/python\" \"$SSD_PATH/main.py\""
echo ""
echo -e "  ${BOLD}Audiodateien ablegen in:${RESET}"
echo -e "    $SSD_PATH/input/"
echo ""
echo -e "${GREEN}${BOLD}================================================${RESET}"
echo ""

# ==========================================
# Optional: als macOS-Dienst einrichten
# ==========================================
if [ -x "$REPO_DIR/service.sh" ]; then
    echo -e "${BOLD}Automatisch beim Login starten?${RESET}"
    echo -e "  Richtet einen LaunchAgent ein, der im Hintergrund laeuft und"
    echo -e "  bei jedem Login automatisch startet. Stoppen/Entfernen jederzeit"
    echo -e "  per ${BOLD}./service.sh stop${RESET} bzw. ${BOLD}./service.sh uninstall${RESET}."
    echo -n "  Einrichten? [j/N]: "
    read -r SETUP_SERVICE
    if [[ "$SETUP_SERVICE" =~ ^[jJyY]$ ]]; then
        # start.sh muss existieren und ausfuehrbar sein, damit der Agent funktioniert
        if [ ! -x "$REPO_DIR/start.sh" ]; then
            warn "start.sh fehlt oder ist nicht ausfuehrbar -- Service-Setup uebersprungen."
        else
            "$REPO_DIR/service.sh" install
        fi
    else
        echo -e "${DIM}[SKIP]${RESET} Service-Setup uebersprungen."
        echo -e "  Spaeter nachholen mit: ${BOLD}./service.sh install${RESET}"
    fi
fi
