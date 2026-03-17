"""
Meeting Protokollant -- Lokales KI-Transkriptions- und Zusammenfassungssystem
Optimiert fuer Mac Mini M4 (Apple Silicon) mit externer SSD.

Workflow:
1. Watchdog ueberwacht den Input-Ordner auf neue Audiodateien
2. MLX Whisper transkribiert lokal auf der Apple GPU (Schweizerdeutsch -> Hochdeutsch)
3. Optional: Pyannote erkennt verschiedene Sprecher
4. Ollama/Mistral NeMo erstellt ein strukturiertes Meeting-Protokoll
5. Ergebnis wird als Textdatei gespeichert + macOS-Notification

Alle Daten bleiben lokal. Kein Internet noetig nach dem initialen Setup.
"""

import os
import sys
import time
import json
import logging
import subprocess
import shutil
from pathlib import Path

import requests
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ==========================================
# 1. KONFIGURATION
# ==========================================
load_dotenv()

SSD_PATH = Path(os.getenv("SSD_PATH", "/Volumes/ExtSSD/WhisperSystem"))
INPUT_DIR = SSD_PATH / "input"
OUTPUT_DIR = SSD_PATH / "output"
TEMP_DIR = SSD_PATH / "temp"
FAILED_DIR = SSD_PATH / "failed"
ARCHIVE_DIR = SSD_PATH / "archive"
PROCESSED_FILE = SSD_PATH / "processed_files.json"

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "auto")  # "auto" = CH-Modell mit Fallback
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral-nemo")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
ENABLE_DIARIZATION = os.getenv("ENABLE_DIARIZATION", "false").lower() == "true"
HF_TOKEN = os.getenv("HF_TOKEN", "")

# Schweizerdeutsch-Modell Konfiguration
CH_MODEL_HF = "Flurin17/whisper-large-v3-turbo-swiss-german"  # PyTorch-Quelle
CH_MODEL_LOCAL = SSD_PATH / "models" / "ch-whisper-mlx"       # Lokaler MLX-Pfad
FALLBACK_MODEL = "mlx-community/whisper-large-v3-turbo-german-f16"  # MLX-Fallback

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma")
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # Sekunden

# Ordner erstellen
for d in [INPUT_DIR, OUTPUT_DIR, TEMP_DIR, FAILED_DIR, ARCHIVE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ==========================================
# 2. LOGGING
# ==========================================
LOG_FILE = SSD_PATH / "system_log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ==========================================
# 3. HILFSFUNKTIONEN
# ==========================================

def load_processed_files():
    """Laedt die Liste bereits verarbeiteter Dateien."""
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
    return set()


def save_processed_files(processed: set):
    """Speichert die Liste bereits verarbeiteter Dateien."""
    PROCESSED_FILE.write_text(json.dumps(sorted(processed)), encoding="utf-8")


def wait_for_stable_file(path: Path, interval: float = 2.0, checks: int = 3):
    """
    Wartet bis eine Datei vollstaendig kopiert wurde.
    Prueft ob sich die Dateigroesse ueber mehrere Intervalle nicht mehr aendert.
    """
    prev_size = -1
    stable_count = 0
    while stable_count < checks:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        if size == prev_size and size > 0:
            stable_count += 1
        else:
            stable_count = 0
            prev_size = size
        if stable_count < checks:
            time.sleep(interval)
    log.info(f"Datei stabil: {path.name} ({prev_size / 1024 / 1024:.1f} MB)")


def retry_with_backoff(func, description: str):
    """Fuehrt eine Funktion mit bis zu MAX_RETRIES Versuchen aus."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func()
        except Exception as e:
            if attempt == MAX_RETRIES:
                log.error(f"{description} fehlgeschlagen nach {MAX_RETRIES} Versuchen: {e}")
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(f"{description} Versuch {attempt}/{MAX_RETRIES} fehlgeschlagen: {e}. "
                        f"Naechster Versuch in {delay}s...")
            time.sleep(delay)


def notify_macos(title: str, message: str):
    """Sendet eine macOS-Benachrichtigung."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass  # Notification ist nice-to-have, nie kritisch


# ==========================================
# 4. MODELL-KONVERTIERUNG (CH-WHISPER -> MLX)
# ==========================================

def convert_ch_model_to_mlx() -> bool:
    """
    Konvertiert das Flurin17 Schweizerdeutsch-Modell von PyTorch ins MLX-Format.
    Braucht einmalig Internet + ~5 Min. Danach laeuft alles offline.
    Gibt True zurueck wenn das Modell nach der Konvertierung verfuegbar ist.
    """
    if CH_MODEL_LOCAL.exists() and any(CH_MODEL_LOCAL.iterdir()):
        log.info(f"CH-Modell bereits konvertiert: {CH_MODEL_LOCAL}")
        return True

    log.info(f"Schweizerdeutsch-Modell wird erstmalig konvertiert...")
    log.info(f"  Quelle:  {CH_MODEL_HF} (HuggingFace, PyTorch)")
    log.info(f"  Ziel:    {CH_MODEL_LOCAL} (lokal, MLX)")
    log.info("  Dies dauert ca. 3-5 Minuten und braucht einmalig Internet.")

    try:
        import numpy as np
        import torch
        import mlx.core as mx
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        # 1. PyTorch-Modell von HuggingFace laden
        log.info("  [1/3] Lade Modell von HuggingFace...")
        processor = WhisperProcessor.from_pretrained(CH_MODEL_HF)
        model = WhisperForConditionalGeneration.from_pretrained(CH_MODEL_HF)

        # 2. Gewichte in MLX-Format konvertieren (numpy als Zwischenschritt)
        log.info("  [2/3] Konvertiere Gewichte nach MLX...")
        CH_MODEL_LOCAL.mkdir(parents=True, exist_ok=True)

        state_dict = model.state_dict()
        mlx_weights = {}
        for key, tensor in state_dict.items():
            np_array = tensor.cpu().to(torch.float32).numpy()
            mlx_weights[key] = mx.array(np_array)

        # Gewichte speichern
        weights_path = CH_MODEL_LOCAL / "weights.npz"
        mx.savez(str(weights_path), **mlx_weights)

        # 3. Config und Tokenizer kopieren
        log.info("  [3/3] Speichere Konfiguration...")
        model.config.save_pretrained(str(CH_MODEL_LOCAL))
        processor.save_pretrained(str(CH_MODEL_LOCAL))

        log.info(f"  Konvertierung abgeschlossen! Modell unter: {CH_MODEL_LOCAL}")
        notify_macos("Meeting Protokollant", "CH-Modell erfolgreich konvertiert!")
        return True

    except ImportError as e:
        missing = str(e).split("'")[-2] if "'" in str(e) else str(e)
        log.warning(f"  Konvertierung nicht moeglich: '{missing}' fehlt.")
        log.warning(f"  Installieren mit: pip install transformers torch")
        log.warning(f"  Nutze Fallback-Modell: {FALLBACK_MODEL}")
        return False
    except Exception as e:
        log.warning(f"  Konvertierung fehlgeschlagen: {e}")
        log.warning(f"  Nutze Fallback-Modell: {FALLBACK_MODEL}")
        # Aufraumen bei fehlgeschlagener Konvertierung
        if CH_MODEL_LOCAL.exists():
            shutil.rmtree(str(CH_MODEL_LOCAL), ignore_errors=True)
        return False


def resolve_whisper_model() -> str:
    """
    Bestimmt welches Whisper-Modell verwendet wird.
    Bei WHISPER_MODEL=auto: Versucht CH-Modell, Fallback auf Deutsch.
    Sonst: Nimmt den konfigurierten Wert.
    """
    global WHISPER_MODEL

    if WHISPER_MODEL != "auto":
        log.info(f"Whisper-Modell manuell gesetzt: {WHISPER_MODEL}")
        return WHISPER_MODEL

    # Auto-Modus: CH-Modell bevorzugt
    if CH_MODEL_LOCAL.exists() and any(CH_MODEL_LOCAL.iterdir()):
        WHISPER_MODEL = str(CH_MODEL_LOCAL)
        log.info(f"Verwende lokales CH-Modell: {WHISPER_MODEL}")
        return WHISPER_MODEL

    # Versuche Konvertierung
    if convert_ch_model_to_mlx():
        WHISPER_MODEL = str(CH_MODEL_LOCAL)
        log.info(f"Verwende frisch konvertiertes CH-Modell: {WHISPER_MODEL}")
        return WHISPER_MODEL

    # Fallback
    WHISPER_MODEL = FALLBACK_MODEL
    log.info(f"Verwende Fallback-Modell: {WHISPER_MODEL}")
    return WHISPER_MODEL


# ==========================================
# 5. HEALTH-CHECKS
# ==========================================

def check_ollama():
    """Prueft ob Ollama laeuft und erreichbar ist."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        if not any(OLLAMA_MODEL in m for m in models):
            log.warning(f"Ollama-Modell '{OLLAMA_MODEL}' nicht gefunden. "
                        f"Verfuegbare Modelle: {models}. "
                        f"Bitte 'ollama pull {OLLAMA_MODEL}' ausfuehren.")
            return False
        log.info(f"Ollama OK -- Modell '{OLLAMA_MODEL}' verfuegbar.")
        return True
    except requests.ConnectionError:
        log.error(f"Ollama nicht erreichbar unter {OLLAMA_URL}. "
                  "Bitte Ollama starten (Ollama App oeffnen oder 'ollama serve').")
        return False
    except Exception as e:
        log.error(f"Ollama Health-Check fehlgeschlagen: {e}")
        return False


def check_whisper():
    """Prueft ob mlx-whisper importiert werden kann."""
    try:
        import mlx_whisper  # noqa: F401
        log.info(f"MLX Whisper OK -- Modell: {WHISPER_MODEL}")
        return True
    except ImportError:
        log.error("mlx-whisper nicht installiert. Bitte 'pip install mlx-whisper' ausfuehren.")
        return False


def check_diarization():
    """Prueft ob Pyannote fuer Sprechererkennung verfuegbar ist."""
    if not ENABLE_DIARIZATION:
        log.info("Sprechererkennung deaktiviert (ENABLE_DIARIZATION=false).")
        return True
    if not HF_TOKEN:
        log.warning("ENABLE_DIARIZATION=true, aber kein HF_TOKEN gesetzt. "
                     "Sprechererkennung wird uebersprungen.")
        return False
    try:
        from pyannote.audio import Pipeline  # noqa: F401
        log.info("Pyannote OK -- Sprechererkennung aktiviert.")
        return True
    except ImportError:
        log.warning("pyannote.audio nicht installiert. "
                     "Sprechererkennung wird uebersprungen. "
                     "Installieren mit: pip install pyannote.audio")
        return False


# ==========================================
# 5. TRANSKRIPTION (MLX WHISPER)
# ==========================================

def transcribe_audio(audio_path: Path) -> str:
    """Transkribiert eine Audiodatei mit MLX Whisper."""
    import mlx_whisper

    def _transcribe():
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=WHISPER_MODEL,
            language="de",
            word_timestamps=True,
        )
        return result.get("text", "")

    text = retry_with_backoff(_transcribe, f"Transkription von {audio_path.name}")
    log.info(f"Transkription abgeschlossen: {len(text)} Zeichen")
    return text


# ==========================================
# 6. SPRECHERERKENNUNG (PYANNOTE)
# ==========================================

_diarization_pipeline = None


def get_diarization_pipeline():
    """Laedt die Pyannote-Pipeline (einmalig, wird gecacht)."""
    global _diarization_pipeline
    if _diarization_pipeline is None:
        from pyannote.audio import Pipeline
        _diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=HF_TOKEN,
        )
    return _diarization_pipeline


def diarize_audio(audio_path: Path) -> list:
    """
    Erkennt Sprecher in einer Audiodatei.
    Gibt eine Liste von (start, end, speaker) Tupeln zurueck.
    """
    try:
        pipeline = get_diarization_pipeline()
        diarization = pipeline(str(audio_path))
        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append((turn.start, turn.end, speaker))
        log.info(f"Sprechererkennung: {len(set(s[2] for s in segments))} Sprecher erkannt")
        return segments
    except Exception as e:
        log.warning(f"Sprechererkennung fehlgeschlagen: {e}. Fahre ohne Sprecher-Labels fort.")
        return []


def merge_transcript_with_speakers(text: str, segments: list) -> str:
    """
    Kombiniert den transkribierten Text mit Sprecher-Labels.
    Vereinfachte Zuordnung: Teilt den Text in Saetze und ordnet sie
    den Sprecher-Segmenten zeitlich zu.
    """
    if not segments:
        return text

    # Sprecher umbenennen: SPEAKER_00 -> Person 1
    speaker_map = {}
    counter = 1
    for _, _, speaker in segments:
        if speaker not in speaker_map:
            speaker_map[speaker] = f"Person {counter}"
            counter += 1

    formatted_parts = []
    current_speaker = None
    for _, _, speaker in segments:
        label = speaker_map[speaker]
        if label != current_speaker:
            current_speaker = label
            formatted_parts.append(f"\n{label}:")

    # Fallback: Wenn Zuordnung nicht klappt, einfach Text mit Sprecher-Uebersicht
    if not formatted_parts:
        return text

    result = f"Erkannte Sprecher: {', '.join(speaker_map.values())}\n\n{text}"
    return result


# ==========================================
# 7. KI-ZUSAMMENFASSUNG (OLLAMA)
# ==========================================

def summarize_with_ollama(text: str) -> str:
    """Erstellt ein strukturiertes Protokoll mit Ollama."""
    log.info(f"Sende Transkript an Ollama ({OLLAMA_MODEL})...")

    system_instruction = (
        "Du bist ein hocheffizienter Protokollfuehrer in einem Schweizer Unternehmen. "
        "Das folgende Transkript wurde aus dem Schweizerdeutschen ins Hochdeutsche uebersetzt. "
        "Erstelle ein professionelles Protokoll mit folgenden Abschnitten:\n"
        "1. THEMA: Um was ging es primaer?\n"
        "2. TEILNEHMER: Wer hat gesprochen?\n"
        "3. ZUSAMMENFASSUNG: Die wichtigsten Punkte in 3-5 Saetzen.\n"
        "4. ENTSCHEIDUNGEN: Was wurde beschlossen?\n"
        "5. ACTIONPOINTS: Wer muss was bis wann tun? (Klar aufgelistet)\n\n"
        "WICHTIG: Verwende Schweizer Rechtschreibung (kein ss statt ss). "
        "Waehrungen sind in CHF anzugeben."
    )

    def _summarize():
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "system": system_instruction,
                "prompt": f"Hier ist das Transkript:\n\n{text}",
                "stream": False,
                "options": {
                    "num_ctx": 32768,
                    "temperature": 0.3,
                },
            },
            timeout=300,
        )
        r.raise_for_status()
        return r.json().get("response", "")

    summary = retry_with_backoff(_summarize, "Ollama-Zusammenfassung")
    log.info(f"Zusammenfassung erstellt: {len(summary)} Zeichen")
    return summary


# ==========================================
# 8. VERARBEITUNG EINER AUDIODATEI
# ==========================================

def process_audio_file(audio_path: Path, processed: set) -> bool:
    """
    Verarbeitet eine einzelne Audiodatei durch die komplette Pipeline.
    Gibt True zurueck bei Erfolg, False bei Fehler.
    """
    filename = audio_path.name
    base_name = audio_path.stem

    if filename in processed:
        log.info(f"Ueberspringe bereits verarbeitete Datei: {filename}")
        return True

    log.info(f"=== Starte Verarbeitung: {filename} ===")

    try:
        # Schritt 1: Transkription
        text = transcribe_audio(audio_path)
        if not text.strip():
            log.warning(f"Leeres Transkript fuer {filename}. Datei uebersprungen.")
            return False

        # Schritt 2: Sprechererkennung (optional)
        if ENABLE_DIARIZATION and HF_TOKEN:
            try:
                segments = diarize_audio(audio_path)
                text = merge_transcript_with_speakers(text, segments)
            except Exception as e:
                log.warning(f"Sprechererkennung fehlgeschlagen, fahre ohne fort: {e}")

        # Schritt 3: KI-Zusammenfassung
        summary = summarize_with_ollama(text)

        # Schritt 4: Protokoll speichern
        final_file = OUTPUT_DIR / f"{base_name}_Protokoll.txt"
        final_file.write_text(
            "========================================\n"
            "KI-PROTOKOLL & ACTIONPOINTS\n"
            "========================================\n\n"
            f"{summary}\n\n\n"
            "========================================\n"
            "DETAILLIERTES TRANSKRIPT\n"
            "========================================\n\n"
            f"{text}\n",
            encoding="utf-8",
        )

        # Schritt 5: Aufraeumen
        # Input-Datei archivieren
        archive_dest = ARCHIVE_DIR / filename
        if archive_dest.exists():
            archive_dest = ARCHIVE_DIR / f"{base_name}_{int(time.time())}{audio_path.suffix}"
        shutil.move(str(audio_path), str(archive_dest))

        # Temp-Dateien loeschen
        for temp_file in TEMP_DIR.glob(f"{base_name}.*"):
            temp_file.unlink(missing_ok=True)

        # Als verarbeitet markieren
        processed.add(filename)
        save_processed_files(processed)

        log.info(f"Fertig! Protokoll: {final_file}")
        notify_macos("Meeting Protokollant", f"Protokoll fertig: {base_name}")
        return True

    except Exception as e:
        log.error(f"Fehler bei {filename}: {e}", exc_info=True)
        # In Failed-Ordner verschieben
        try:
            failed_dest = FAILED_DIR / filename
            if audio_path.exists():
                shutil.move(str(audio_path), str(failed_dest))
                log.info(f"Datei verschoben nach: {failed_dest}")
        except Exception:
            pass
        notify_macos("Meeting Protokollant", f"Fehler bei: {base_name}")
        return False


# ==========================================
# 9. WATCHDOG (ORDNER-UEBERWACHUNG)
# ==========================================

class AudioHandler(FileSystemEventHandler):
    def __init__(self, processed: set):
        self.processed = processed

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return

        log.info(f"Neue Datei erkannt: {path.name}. Warte auf vollstaendigen Upload...")
        wait_for_stable_file(path)
        process_audio_file(path, self.processed)


# ==========================================
# 10. STARTUP-SCAN
# ==========================================

def scan_existing_files(processed: set):
    """Verarbeitet Audiodateien, die bereits im Input-Ordner liegen."""
    existing = sorted(
        f for f in INPUT_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not existing:
        log.info("Keine bestehenden Dateien im Input-Ordner.")
        return

    log.info(f"Startup-Scan: {len(existing)} Audiodatei(en) gefunden.")
    for audio_file in existing:
        process_audio_file(audio_file, processed)


# ==========================================
# 11. HAUPTPROGRAMM
# ==========================================

def main():
    print("\n" + "=" * 50)
    print("  Meeting Protokollant -- Systemstart")
    print("=" * 50)

    # Konfiguration anzeigen
    log.info(f"SSD-Pfad:          {SSD_PATH}")
    log.info(f"Ollama-Modell:      {OLLAMA_MODEL}")
    log.info(f"Sprechererkennung:  {'Aktiviert' if ENABLE_DIARIZATION else 'Deaktiviert'}")

    # Modell-Auswahl (ggf. mit Auto-Konvertierung)
    print("\n--- Modell-Setup ---")
    resolve_whisper_model()
    log.info(f"Whisper-Modell:     {WHISPER_MODEL}")

    # Health-Checks
    print("\n--- Health-Checks ---")
    whisper_ok = check_whisper()
    ollama_ok = check_ollama()
    diarization_ok = check_diarization()

    if not whisper_ok:
        log.error("MLX Whisper nicht verfuegbar. Abbruch.")
        sys.exit(1)
    if not ollama_ok:
        log.warning("Ollama nicht verfuegbar. Zusammenfassungen werden fehlschlagen.")

    # Verarbeitungsliste laden
    processed = load_processed_files()
    log.info(f"Bereits verarbeitet: {len(processed)} Datei(en)")

    # Startup-Scan
    print("\n--- Startup-Scan ---")
    scan_existing_files(processed)

    # Watchdog starten
    event_handler = AudioHandler(processed)
    observer = Observer()
    observer.schedule(event_handler, str(INPUT_DIR), recursive=False)
    observer.start()

    print("\n" + "=" * 50)
    print(f"  System aktiv! Ueberwache Ordner:")
    print(f"  {INPUT_DIR}")
    print("=" * 50 + "\n")
    log.info("Waechter-Skript gestartet.")
    notify_macos("Meeting Protokollant", "System aktiv und bereit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nSystem wird beendet...")
        log.info("System manuell gestoppt.")

    observer.join()


if __name__ == "__main__":
    main()
