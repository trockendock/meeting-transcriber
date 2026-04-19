# Meeting Protokollant -- Setup-Guide

Lokales KI-System für automatische Meeting-Transkription und Protokollerstellung.
Läuft komplett auf deinem Mac -- kein Internet nötig nach dem Setup.

> **Tipp:** Das Installer-Script `install.sh` automatisiert alle untenstehenden Schritte.
> Siehe [README.md](README.md#schnellstart) für die schnelle Variante.

## Voraussetzungen

- **Mac mit Apple Silicon** (M1, M2, M3 oder M4)
- **16 GB RAM** (Minimum)
- **Homebrew** installiert ([brew.sh](https://brew.sh))
- **Externe SSD** (empfohlen: APFS-formatiert fuer beste Watchdog-Performance)
- ~10 GB freier Speicher fuer Modelle und Software

---

## Schritt 1: Grundlagen installieren

```bash
# Homebrew aktualisieren
brew update

# Pyenv fuer update-sichere Python-Version
brew install pyenv

# FFmpeg installieren (wird fuer Audio-Verarbeitung benoetigt)
brew install ffmpeg

# Python 3.11 installieren (stabil fuer ML-Pakete)
pyenv install 3.11.9

# pyenv in der aktuellen Shell aktivieren
eval "$(pyenv init -)"

# Ollama installieren (lokale KI-Engine)
brew install --cask ollama
```

> **Wichtig:** Damit der `python`-Befehl dauerhaft funktioniert, fuege diese Zeile in deine `~/.zshrc` ein:
> ```bash
> echo 'eval "$(pyenv init -)"' >> ~/.zshrc
> ```

**Ollama starten:** Oeffne die Ollama-App einmal. Sie laeuft danach im Hintergrund.
Du erkennst es am Llama-Icon in der Menüleiste oben rechts.

```bash
# Ollama starten (falls noch nicht offen)
open -a Ollama

# Zusammenfassungs-Modell herunterladen (~4 GB)
ollama pull mistral-nemo
```

---

## Schritt 2: Projektordner auf der SSD einrichten

Ersetze `ExtSSD` durch den Namen deiner Festplatte.

```bash
# Ordner erstellen
mkdir -p /Volumes/ExtSSD/WhisperSystem
cd /Volumes/ExtSSD/WhisperSystem

# Python-Version festlegen
pyenv local 3.11.9

# Virtual Environment erstellen und aktivieren
python -m venv venv
source venv/bin/activate

# Pakete installieren (Basis)
pip install mlx-whisper python-dotenv watchdog requests

# Fuer die automatische Konvertierung des Schweizerdeutsch-Modells:
pip install transformers torch
```

> **Hinweis:** `transformers` und `torch` werden nur einmalig fuer die Modell-Konvertierung
> benoetigt (~2 GB). Beim ersten Start konvertiert das Skript automatisch das
> Schweizerdeutsch-Modell (Flurin17/whisper-large-v3-turbo-swiss-german) ins MLX-Format.
> Danach laufen alle weiteren Starts ohne diese Pakete.
> Falls du die Konvertierung ueberspringen willst, setze in `.env`:
> `WHISPER_MODEL=mlx-community/whisper-large-v3-turbo-german-f16`

### Optional: Sprechererkennung

Wenn du wissen willst, WER was gesagt hat (z.B. "Person 1: ..., Person 2: ..."):

```bash
pip install pyannote.audio
```

Dafuer brauchst du einen **Hugging Face Token** (kostenlos):
1. Account erstellen auf [huggingface.co](https://huggingface.co)
2. Token erstellen: [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (Typ: "Read")
3. Nutzungsbedingungen akzeptieren:
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)

---

## Schritt 3: Konfiguration (.env)

Kopiere die Vorlage und passe sie an:

```bash
cp .env.example .env
```

Oeffne `.env` in einem Texteditor und aendere mindestens:

```
SSD_PATH=/Volumes/ExtSSD/WhisperSystem    # <- Dein SSD-Name!
```

Falls du Sprechererkennung willst:
```
ENABLE_DIARIZATION=true
HF_TOKEN=hf_dein_token_hier
```

---

## Schritt 4: Erster Test

```bash
# Ins Projektverzeichnis wechseln und venv aktivieren
cd /Volumes/ExtSSD/WhisperSystem
source venv/bin/activate

# Skript starten
python main.py
```

Du solltest sehen:
```
==================================================
  Meeting Protokollant -- Systemstart
==================================================
... Konfiguration ...
--- Health-Checks ---
... MLX Whisper OK ...
... Ollama OK ...
--- Startup-Scan ---
... Keine bestehenden Dateien ...
==================================================
  System aktiv! Ueberwache Ordner:
  /Volumes/ExtSSD/WhisperSystem/input
==================================================
```

**Teste es:** Kopiere eine MP3-, WAV- oder M4A-Datei in den `input/`-Ordner.
Das Skript erkennt die Datei automatisch, transkribiert sie und erstellt ein Protokoll in `output/`.

---

## Schritt 5: Starten

Drei Wege, geordnet von minimal zu komfortabel. Alle drei führen zum gleichen
Resultat – nur der Auto-Start-Komfort variiert.

### Option A: Doppelklick

`Meeting Transcriber.command` im Finder doppelklicken. Öffnet ein
Terminal-Fenster, in dem das Log live mitläuft. Beim Schließen des Fensters
stoppt der Dienst.

Passend für: gelegentliche Nutzung, Live-Einblick ins Log.

### Option B: macOS-Dienst *(empfohlen)*

Startet automatisch beim Login, läuft still im Hintergrund, startet nach
Crash neu:

```bash
./service.sh install      # einmalig einrichten (fragt nicht nach)
./service.sh status       # laeuft? PID, state, letzter Exit-Code
./service.sh logs         # tail -F ueber stdout/stderr/system_log.txt
./service.sh stop         # SIGTERM, sauberer Shutdown
./service.sh restart
./service.sh uninstall    # plist entfernen, agent entladen
```

Der Installer (`install.sh`) bietet das Service-Setup am Ende an.

Unter der Haube: LaunchAgent in `~/Library/LaunchAgents/ch.trockendock.meetingtranscriber.plist`
mit `KeepAlive.SuccessfulExit=false` + `ThrottleInterval=30` (Crash → Neustart
nach 30 s, manueller Stop → bleibt gestoppt).

Passend für: "einmal einrichten, nie wieder drüber nachdenken".

### Option C: Manuell via Terminal

```bash
./start.sh                # startet Ollama + venv + main.py
# oder direkt:
source "$SSD_PATH/venv/bin/activate"
python "$SSD_PATH/main.py"
```

Passend für: Debugging, Development.

---

## Ordnerstruktur

Nach dem Setup sieht deine SSD so aus:

```
/Volumes/ExtSSD/WhisperSystem/
    .env                        # Deine Konfiguration
    .env.example                # Vorlage
    main.py                     # Das Hauptskript
    venv/                       # Python-Umgebung
    models/ch-whisper-mlx/      # Konvertiertes CH-Whisper-Modell
    system_log.txt              # Rotiert bei 10 MB (5 Backups)
    service_stdout.log          # Nur bei Service-Betrieb
    service_stderr.log          # Nur bei Service-Betrieb
    processed_files.json        # Liste verarbeiteter Dateien
    input/                      # Audiodateien hier reinlegen
    output/                     # Fertige Protokolle
    archive/                    # Verarbeitete Audiodateien (auto-retention)
    temp/                       # Zwischendateien (werden aufgeraeumt)
    failed/                     # Fehlgeschlagene Dateien (auto-retention)
```

Im Git-Repository (dort wo du `./install.sh` aufrufst):

```
meeting-transcriber/
    install.sh                  # Setup-Installer
    start.sh                    # manueller Start
    service.sh                  # LaunchAgent-Manager
    Meeting Transcriber.command # Doppelklick-Starter
    main.py                     # Quelle (wird beim install kopiert)
    .env.example
    README.md
    setup-guide.md
    CHANGELOG.md
```

---

## Troubleshooting

### "Ollama nicht erreichbar"
```bash
# Pruefen ob Ollama laeuft
curl http://localhost:11434/api/tags

# Falls nicht: Ollama starten
open -a Ollama
# oder
ollama serve
```

### "MLX Whisper nicht installiert"
```bash
cd /Volumes/ExtSSD/WhisperSystem
source venv/bin/activate
pip install mlx-whisper
```

### "Modell nicht gefunden"
Beim ersten Start laed mlx-whisper das Modell automatisch herunter (~1.5 GB).
Das braucht einmalig Internet und kann einige Minuten dauern.

```bash
# Ollama-Modell manuell laden
ollama pull mistral-nemo
```

### "Sprechererkennung fehlt"
```bash
# Pyannote installieren
pip install pyannote.audio

# In .env setzen:
# ENABLE_DIARIZATION=true
# HF_TOKEN=hf_dein_token
```

### "command not found: python" nach pyenv install
```bash
# pyenv in der Shell aktivieren
eval "$(pyenv init -)"

# Dauerhaft in ~/.zshrc eintragen
echo 'eval "$(pyenv init -)"' >> ~/.zshrc
```

### "Could not load libtorchcodec" / FFmpeg-Warnungen
```bash
# FFmpeg installieren
brew install ffmpeg
```
Diese Warnung ist nicht kritisch -- die Transkription funktioniert auch ohne.
Aber fuer beste Audio-Kompatibilitaet sollte FFmpeg installiert sein.

### "Kein Sound erkannt / Leeres Transkript"
- Unterstuetzte Formate: MP3, WAV, M4A, AAC, FLAC, OGG, WMA
- SSD sollte APFS-formatiert sein (nicht exFAT) -- Festplattendienstprogramm > Info
- Datei vollstaendig kopiert? Das Skript wartet automatisch, aber bei sehr grossen Dateien via Netzwerk kann es Probleme geben

### Logs einsehen
```bash
# App-Log (Main-Output)
tail -f /Volumes/ExtSSD/WhisperSystem/system_log.txt

# Bei Service-Betrieb zusaetzlich:
tail -f /Volumes/ExtSSD/WhisperSystem/service_stderr.log

# Komfortabel: alle Logs gleichzeitig
./service.sh logs
```

### Service startet nicht / bleibt nach Crash nicht oben
```bash
# Detailierter State des Agents
launchctl print "gui/$(id -u)/ch.trockendock.meetingtranscriber"

# Komplett neu laden
./service.sh uninstall
./service.sh install

# Manuelles Debug: start.sh direkt ausfuehren, um Fehler zu sehen
./start.sh
```

Typische Ursachen:
- SSD nicht gemountet (Agent startet, `start.sh` bricht ab, Neustart nach 30 s)
- Ollama.app nicht erreichbar (Gatekeeper hat sie beim ersten Start nicht zugelassen)
- `.env` fehlt im Repo-Ordner (hat `start.sh` keinen `SSD_PATH`)

---

## Fuer Power-User: Modell-Optionen

### Automatische Konvertierung (Standard)

Mit `WHISPER_MODEL=auto` in `.env` passiert folgendes beim ersten Start:
1. Das Schweizerdeutsch-Modell (`Flurin17/whisper-large-v3-turbo-swiss-german`) wird von HuggingFace geladen
2. Die Gewichte werden automatisch ins MLX-Format konvertiert
3. Das konvertierte Modell wird unter `models/ch-whisper-mlx/` gespeichert
4. Ab dem zweiten Start wird das lokale Modell direkt geladen (kein Internet noetig)

Voraussetzung: `pip install transformers torch` (einmalig, ~2 GB)

### Andere Schweizerdeutsch-Modelle testen

Du kannst auch andere Modelle konvertieren. Dazu in `main.py` die Variable `CH_MODEL_HF` aendern:

```python
CH_MODEL_HF = "nizarmichaud/whisper-large-v3-turbo-swissgerman"  # QLoRa-optimiert
```

Dann den `models/ch-whisper-mlx/`-Ordner loeschen und das Skript neu starten.

Verfuegbare Modelle:
- `Flurin17/whisper-large-v3-turbo-swiss-german` -- FHNW Swiss Parliament Corpus (Standard)
- `nizarmichaud/whisper-large-v3-turbo-swissgerman` -- QLoRa-optimiert fuer CH-Dialekte
- `Flurin17/whisper-large-v3-peft-swiss-german` -- PEFT/LoRA Adapter (speichereffizient)
