#!/usr/bin/env bash
set -euo pipefail

# ==========================================
# Meeting Transcriber -- macOS Service-Manager
# ==========================================
# Richtet das System als LaunchAgent ein, damit es:
#   - automatisch beim Login startet
#   - nach Crash automatisch neu startet
#   - still im Hintergrund laeuft (kein Terminal-Fenster)
#
# Verwendung:
#   ./service.sh install    Dienst einrichten und starten
#   ./service.sh uninstall  Dienst deaktivieren und entfernen
#   ./service.sh start      Dienst jetzt starten
#   ./service.sh stop       Dienst sauber stoppen (SIGTERM)
#   ./service.sh restart    stop + start
#   ./service.sh reload     plist neu laden (nach Aenderungen)
#   ./service.sh status     Laufstatus und letzter Exit-Code
#   ./service.sh logs       Live-Log anzeigen (Ctrl-C zum Beenden)
# ==========================================

# --- Farben ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()    { echo -e "${BLUE}[INFO]${RESET} $1"; }
success() { echo -e "${GREEN}[OK]${RESET}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $1"; }
err()     { echo -e "${RED}[FEHLER]${RESET} $1" >&2; }
die()     { err "$1"; exit 1; }

# --- Konstanten ---
LABEL="ch.trockendock.meetingtranscriber"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SCRIPT="$SCRIPT_DIR/start.sh"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="$DOMAIN/$LABEL"

# SSD_PATH aus .env lesen (gleiche Logik wie start.sh)
load_ssd_path() {
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        SSD_PATH="$(grep -E '^SSD_PATH=' "$SCRIPT_DIR/.env" 2>/dev/null | head -1 | cut -d'=' -f2- || true)"
    fi
    SSD_PATH="${SSD_PATH:-/Volumes/ExtSSD/WhisperSystem}"
}

log_paths() {
    load_ssd_path
    SERVICE_LOG="$SSD_PATH/service_stdout.log"
    SERVICE_ERR="$SSD_PATH/service_stderr.log"
}

# --- Launchctl-Wrapper (modernes API bevorzugt, Fallback auf load/unload) ---
lc_load() {
    if launchctl bootstrap "$DOMAIN" "$PLIST_PATH" 2>/dev/null; then
        return 0
    fi
    launchctl load -w "$PLIST_PATH"
}

lc_unload() {
    if launchctl bootout "$SERVICE_TARGET" 2>/dev/null; then
        return 0
    fi
    # -w merkt sich den "disabled"-State, damit er nicht still wieder startet
    launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
}

lc_kickstart() {
    if launchctl kickstart -k "$SERVICE_TARGET" 2>/dev/null; then
        return 0
    fi
    launchctl start "$LABEL"
}

lc_stop() {
    # SIGTERM an main.py, damit unser Signal-Handler sauber herunterfaehrt
    if launchctl kill SIGTERM "$SERVICE_TARGET" 2>/dev/null; then
        return 0
    fi
    launchctl stop "$LABEL" 2>/dev/null || true
}

lc_is_loaded() {
    launchctl print "$SERVICE_TARGET" >/dev/null 2>&1
}

# --- plist rendern ---
render_plist() {
    log_paths
    [[ -x "$START_SCRIPT" ]] || die "start.sh nicht ausfuehrbar gefunden: $START_SCRIPT"
    mkdir -p "$PLIST_DIR"
    mkdir -p "$SSD_PATH"  # Log-Pfad muss existieren, sonst startet der Agent nicht

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$START_SCRIPT</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <!-- Bei Crash (non-zero exit) neu starten, bei sauberem Exit NICHT.
         So kann man per 'service.sh stop' anhalten, ohne dass es sofort wieder hochkommt. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Mindestens 30s zwischen Restart-Versuchen (vermeidet Crash-Loops) -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- Ollama ist ein GUI-Cask (open -a Ollama) und braucht die User-Session;
         deshalb LaunchAgent (gui domain), nicht LaunchDaemon. -->
    <key>ProcessType</key>
    <string>Interactive</string>

    <key>StandardOutPath</key>
    <string>$SERVICE_LOG</string>

    <key>StandardErrorPath</key>
    <string>$SERVICE_ERR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <!-- Homebrew (arm64 unter /opt/homebrew, Intel unter /usr/local) + Systempfade. -->
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>de_CH.UTF-8</string>
    </dict>
</dict>
</plist>
PLIST
}

# --- Subcommands ---
cmd_install() {
    info "Label:         $LABEL"
    info "Plist:         $PLIST_PATH"
    info "Start-Skript:  $START_SCRIPT"
    load_ssd_path
    info "SSD-Pfad:      $SSD_PATH"

    if lc_is_loaded; then
        info "Dienst laeuft bereits, starte neu..."
        lc_unload
    fi

    render_plist
    success "plist geschrieben"
    lc_load
    success "Dienst geladen und gestartet"
    echo
    echo -e "  ${BOLD}Status pruefen:${RESET}  ./service.sh status"
    echo -e "  ${BOLD}Logs ansehen:${RESET}    ./service.sh logs"
    echo -e "  ${BOLD}Stoppen:${RESET}         ./service.sh stop"
    echo -e "  ${BOLD}Entfernen:${RESET}       ./service.sh uninstall"
}

cmd_uninstall() {
    if ! lc_is_loaded && [[ ! -f "$PLIST_PATH" ]]; then
        warn "Dienst ist nicht installiert."
        return 0
    fi
    if lc_is_loaded; then
        info "Entlade Dienst..."
        lc_unload
    fi
    if [[ -f "$PLIST_PATH" ]]; then
        rm -f "$PLIST_PATH"
        success "plist entfernt: $PLIST_PATH"
    fi
    success "Dienst deaktiviert."
}

cmd_start() {
    [[ -f "$PLIST_PATH" ]] || die "Dienst nicht installiert. Zuerst: ./service.sh install"
    if ! lc_is_loaded; then
        lc_load
    fi
    lc_kickstart
    success "Dienst gestartet."
}

cmd_stop() {
    lc_is_loaded || { warn "Dienst laeuft nicht."; return 0; }
    info "Sende SIGTERM (sauberer Shutdown)..."
    lc_stop
    success "Stop-Signal gesendet. (Laeuft gerade eine Ollama-Inferenz, kann es bis zu 30s dauern.)"
}

cmd_restart() {
    cmd_stop || true
    sleep 2
    cmd_start
}

cmd_reload() {
    info "Lade plist neu (nach Aenderungen)..."
    if lc_is_loaded; then
        lc_unload
    fi
    render_plist
    lc_load
    success "Dienst neu geladen."
}

cmd_status() {
    if [[ ! -f "$PLIST_PATH" ]]; then
        warn "Dienst ist nicht installiert."
        echo "  Einrichten mit: ./service.sh install"
        exit 1
    fi

    if lc_is_loaded; then
        success "Dienst geladen."
        local out
        out=$(launchctl print "$SERVICE_TARGET" 2>/dev/null || true)
        local pid state exit_code
        pid=$(echo "$out" | grep -E '^\s*pid = ' | awk '{print $3}' | head -1)
        state=$(echo "$out" | grep -E '^\s*state = ' | awk '{print $3}' | head -1)
        exit_code=$(echo "$out" | grep -E '^\s*last exit code = ' | awk '{print $5}' | head -1)
        [[ -n "${state:-}" ]] && echo "  state:          $state"
        [[ -n "${pid:-}" ]]   && echo "  pid:            $pid"
        [[ -n "${exit_code:-}" ]] && echo "  last exit code: $exit_code"
    else
        warn "plist existiert, aber Dienst ist nicht geladen."
        echo "  Starten mit: ./service.sh start"
    fi

    log_paths
    echo
    echo "  Logs:"
    echo "    stdout: $SERVICE_LOG"
    echo "    stderr: $SERVICE_ERR"
    echo "    app:    $SSD_PATH/system_log.txt"
}

cmd_logs() {
    log_paths
    local files=()
    [[ -f "$SERVICE_LOG" ]]               && files+=("$SERVICE_LOG")
    [[ -f "$SERVICE_ERR" ]]               && files+=("$SERVICE_ERR")
    [[ -f "$SSD_PATH/system_log.txt" ]]   && files+=("$SSD_PATH/system_log.txt")

    if [[ ${#files[@]} -eq 0 ]]; then
        warn "Noch keine Log-Dateien vorhanden. Dienst erstmal laufen lassen."
        return 0
    fi

    info "Tail -f auf ${#files[@]} Log-Datei(en) (Ctrl-C zum Beenden)"
    echo
    exec tail -n 50 -F "${files[@]}"
}

cmd_help() {
    cat <<'HELP'
Meeting Transcriber -- macOS Service-Manager

  ./service.sh install    Dienst einrichten und starten (LaunchAgent)
  ./service.sh uninstall  Dienst deaktivieren und entfernen
  ./service.sh start      Dienst jetzt starten
  ./service.sh stop       Dienst sauber stoppen (SIGTERM)
  ./service.sh restart    stop + start
  ./service.sh reload     plist neu laden (nach Aenderungen)
  ./service.sh status     Laufstatus und letzter Exit-Code
  ./service.sh logs       Live-Log anzeigen (Ctrl-C zum Beenden)

Nach dem Install startet der Dienst automatisch bei jedem Login und
laeuft still im Hintergrund. Notifications bleiben sichtbar.
HELP
}

# --- Dispatch ---
case "${1:-help}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    reload)    cmd_reload ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    help|-h|--help) cmd_help ;;
    *)
        err "Unbekanntes Kommando: $1"
        echo
        cmd_help
        exit 1
        ;;
esac
