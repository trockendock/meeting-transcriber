# Meeting Transcriber

Lokales KI-System für automatische Meeting-Transkription und Protokollerstellung auf Apple Silicon Macs. Läuft komplett offline -- kein Byte verlässt dein Gerät.

## Was es tut

1. **Ordner überwachen:** Watchdog erkennt neue Audiodateien im `input/`-Ordner
2. **Transkription:** MLX Whisper transkribiert Schweizerdeutsch → Hochdeutsch direkt auf der Apple GPU
3. **Sprechererkennung** (optional): Pyannote erkennt, wer was gesagt hat
4. **KI-Protokoll:** Ollama/Mistral NeMo erstellt ein strukturiertes Meeting-Protokoll mit Thema, Zusammenfassung, Entscheidungen und Actionpoints
5. **Benachrichtigung:** macOS-Notification wenn das Protokoll fertig ist

## Voraussetzungen

- macOS mit **Apple Silicon** (M1/M2/M3/M4)
- **16 GB RAM** (Minimum)
- [Homebrew](https://brew.sh)
- Externe SSD empfohlen (APFS)

## Schnellstart

```bash
# 1. Abhängigkeiten
brew install pyenv
brew install --cask ollama
pyenv install 3.11.9
ollama pull mistral-nemo

# 2. Projekt einrichten (Pfad anpassen!)
mkdir -p /Volumes/ExtSSD/WhisperSystem
cd /Volumes/ExtSSD/WhisperSystem
pyenv local 3.11.9
python -m venv venv && source venv/bin/activate

# 3. Pakete installieren
pip install mlx-whisper python-dotenv watchdog requests
pip install transformers torch  # für Auto-Konvertierung CH-Modell

# 4. Konfiguration
cp .env.example .env
# -> SSD_PATH in .env anpassen!

# 5. Starten
python main.py
```

Beim ersten Start wird automatisch das Schweizerdeutsch-Modell ([Flurin17/whisper-large-v3-turbo-swiss-german](https://huggingface.co/Flurin17/whisper-large-v3-turbo-swiss-german)) heruntergeladen und ins MLX-Format konvertiert. Danach läuft alles offline.

## Verwendung

Audiodatei (MP3, WAV, M4A, AAC, FLAC) in den `input/`-Ordner kopieren. Das Protokoll erscheint automatisch in `output/`.

```
output/meeting_Protokoll.txt
├── KI-PROTOKOLL & ACTIONPOINTS
│   ├── Thema
│   ├── Teilnehmer
│   ├── Zusammenfassung
│   ├── Entscheidungen
│   └── Actionpoints
└── DETAILLIERTES TRANSKRIPT
```

## Konfiguration (.env)

| Variable | Default | Beschreibung |
|---|---|---|
| `SSD_PATH` | `/Volumes/ExtSSD/WhisperSystem` | Projektordner auf der SSD |
| `WHISPER_MODEL` | `auto` | `auto` = CH-Modell mit Fallback auf Deutsch |
| `OLLAMA_MODEL` | `mistral-nemo` | Modell für die Zusammenfassung |
| `ENABLE_DIARIZATION` | `false` | Sprechererkennung aktivieren |
| `HF_TOKEN` | - | HuggingFace Token (nur für Diarization) |

## Ordnerstruktur

```
WhisperSystem/
├── input/          # Audiodateien hier reinlegen
├── output/         # Fertige Protokolle
├── archive/        # Verarbeitete Audiodateien
├── failed/         # Fehlgeschlagene Dateien
├── temp/           # Zwischendateien (werden aufgeräumt)
├── models/         # Konvertierte MLX-Modelle
├── main.py         # Hauptskript
├── .env            # Konfiguration
└── system_log.txt  # Log
```

## Tech Stack

| Komponente | Zweck | Läuft auf |
|---|---|---|
| [mlx-whisper](https://pypi.org/project/mlx-whisper/) | Transkription (ASR) | Apple GPU (MLX) |
| [Flurin17/whisper-large-v3-turbo-swiss-german](https://huggingface.co/Flurin17/whisper-large-v3-turbo-swiss-german) | Schweizerdeutsch-Erkennung | - |
| [Ollama](https://ollama.com) + Mistral NeMo | Zusammenfassung & Protokoll | Apple GPU |
| [Pyannote](https://github.com/pyannote/pyannote-audio) | Sprechererkennung (optional) | CPU/GPU |
| [Watchdog](https://github.com/gorakhargosh/watchdog) | Ordnerüberwachung | CPU |

## Detaillierter Setup-Guide

Siehe [setup-guide.md](setup-guide.md) für:
- Schritt-für-Schritt Installation
- Automator / Launch Agent Konfiguration
- Troubleshooting
- Alternative Schweizerdeutsch-Modelle

## Datenschutz

Alles läuft lokal. Internet wird nur einmalig beim Setup benötigt (Modelle herunterladen). Danach: Kabel raus, alles funktioniert weiter. Keine Daten verlassen das Gerät.

## Lizenz

MIT
