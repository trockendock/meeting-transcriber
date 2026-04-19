# Changelog

Alle bemerkenswerten Änderungen an diesem Projekt.
Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung folgt [SemVer](https://semver.org/lang/de/).

## [1.1.0] – 2026-04-19

Grosses Refactoring: Performance, Stabilität und Betrieb. Abwärtskompatibel –
vorhandene `.env`-Dateien und Verzeichnisstrukturen laufen weiter.

### Hinzugefügt

- **macOS-Dienst (`service.sh`)** – LaunchAgent-Installer mit
  `install/uninstall/start/stop/restart/status/logs/reload`. System startet
  automatisch beim Login, überlebt Crashes, läuft still im Hintergrund.
- **2-Stufen-Pipeline** (`PIPELINE_PARALLEL=true`, default): Stage 1
  transkribiert Datei N+1, während Stage 2 Datei N per Ollama zusammenfasst
  → ~30–40 % mehr Durchsatz. Backpressure über `SUMMARIZE_QUEUE_MAX`.
- **Retention-Janitor**: räumt `archive/` und `failed/` automatisch auf
  (`ARCHIVE_RETENTION_DAYS`, `FAILED_RETENTION_DAYS`,
  `RETENTION_SWEEP_HOURS`).
- **Crash-Recovery** via `.processing`-Marker: abgebrochene Dateien werden
  beim nächsten Start nach `failed/` verschoben statt endlos wiederholt.
- **Signal-Handling** (SIGTERM, SIGHUP) → sauberer Shutdown auch durch
  `launchctl` oder `kill`.
- **Log-Rotation** (`RotatingFileHandler`, 10 MB × 5 Backups).
- `TRANSCRIPT_CORRECTIONS` nutzt jetzt Regex-Wortgrenzen (`\b`) statt
  Substring-Ersetzung.

### Geändert

- **Sprecher-Zuordnung** komplett neu implementiert: alignt Whisper-Segmente
  über Zeit-Overlap zu Pyannote-Sprechern und fasst zu
  `Person N: ...`-Absätzen zusammen. Vorher wurde das Diarization-Ergebnis
  berechnet, aber nicht in den Text eingebaut (stiller Bug).
- **Watchdog nicht-blockierend**: `on_created`/`on_moved` pusht nur noch in
  die Queue; Worker-Thread erledigt die Arbeit. Mehrfach-Drops blockieren den
  Observer nicht mehr, `on_moved` (Finder-Drag-Drop) wird ebenfalls erkannt.
- **Diarization auf MPS** (Apple GPU via PyTorch) → Faktor 5-10× schneller
  als vorher auf CPU. Fallback auf CPU bei MPS-Fehlern.
- **Ollama `num_ctx` dynamisch** (4k/8k/16k/32k je nach Input-Grösse) statt
  fix 32k → schneller bei kurzen Meetings.
- Whisper-Transkription wird nicht mehr retried: ein Fehler bei Minute 58
  einer 60-Min-Datei zwingt nicht mehr zu 3× Gesamt-Transkription.
- `save_processed_files` schreibt atomar (tmp + `replace`) mit Thread-Lock.
- CH-Modell-Check prüft jetzt explizit `weights.npz` + `config.json` statt
  nur "Ordner nicht leer".

### Behoben

- **AppleScript-Injection** in `notify_macos`: Dateinamen mit `"` oder `\\`
  brechen die Notification nicht mehr ab.
- **`wait_for_stable_file` kann nicht mehr hängen**: 1 h Timeout plus
  separate 0-Byte-Erkennung (60 s) für abgebrochene Uploads.
- **Ollama-Modell-Check**: exakter Match oder `:tag`-Suffix statt
  Substring-Match (`mistral` matchte zuvor `mistral-nemo`).
- **Prompt-Typo** `'ss' statt 'ss'` → `'ss' statt 'ß'`.
- **Dedup** zwischen Watchdog und Workern via gemeinsamem `in_flight`-Set
  mit Lock (statt memory-leakendem Handler-internem Set).

### Entfernt

- Redundanz `max(600, 600 + …)` im Ollama-Timeout (war always no-op).
- Doppelte Imports (`import json as _json`, `import wave, contextlib,
  subprocess` innerhalb von Funktionen).

### Konfiguration (`.env`)

Neue Variablen – alle mit sinnvollen Defaults, keine Aktion nötig:

| Variable | Default | Zweck |
|---|---|---|
| `PIPELINE_PARALLEL` | `true` | 2-Stufen-Pipeline |
| `SUMMARIZE_QUEUE_MAX` | `2` | Backpressure |
| `ARCHIVE_RETENTION_DAYS` | `90` | Archiv-Retention (`0` = aus) |
| `FAILED_RETENTION_DAYS` | `30` | Failed-Retention (`0` = aus) |
| `RETENTION_SWEEP_HOURS` | `24` | Janitor-Intervall |

### Migration

Nach `git pull`:

```bash
./install.sh                    # pyannote-Upgrade falls aktiviert
./service.sh install            # optional: als Hintergrund-Dienst
```

Das neue `main.py` ist drop-in-kompatibel. Bestehende `.env`-Dateien und
`processed_files.json` funktionieren weiter.

---

## [1.0.9] und früher

Siehe `git log --oneline v1.0.9` für die Commit-History vor v1.1.0.
Die Releases v1.0.1–v1.0.9 umfassen die initiale Implementierung sowie
iterative Fixes der CH-Whisper-Konvertierung, Ollama-Prompts und der
Installer-Robustheit.
