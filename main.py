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
from datetime import date
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

# Korrekturliste fuer haeufige Transkriptionsfehler bei Firmennamen / Fachbegriffen
# Format in .env: TRANSCRIPT_CORRECTIONS=Mantelux:Montalux,Strahlhorn:Strahlhorn AG
_raw_corrections = os.getenv("TRANSCRIPT_CORRECTIONS", "")
TRANSCRIPT_CORRECTIONS: dict[str, str] = {}
if _raw_corrections:
    for pair in _raw_corrections.split(","):
        if ":" in pair:
            wrong, correct = pair.split(":", 1)
            TRANSCRIPT_CORRECTIONS[wrong.strip()] = correct.strip()

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

def _hf_to_mlx_key(key: str) -> "str | None":
    """
    Benennt HuggingFace Whisper Gewicht-Keys in mlx_whisper Keys um.
    Gibt None zurueck fuer Keys die kein mlx_whisper-Aequivalent haben.
    """
    import re

    # Top-level fixe Mappings
    # Hinweis: encoder.embed_positions wird bewusst ausgelassen –
    # mlx_whisper berechnet die Encoder-PE als feste Sinusoide (_positional_embedding)
    # und speichert sie nicht als lernbaren Parameter.
    fixed = {
        "encoder.layer_norm.weight":      "encoder.ln_post.weight",
        "encoder.layer_norm.bias":        "encoder.ln_post.bias",
        "decoder.embed_positions.weight": "decoder.positional_embedding",
        "decoder.embed_tokens.weight":    "decoder.token_embedding.weight",
        "decoder.layer_norm.weight":      "decoder.ln.weight",
        "decoder.layer_norm.bias":        "decoder.ln.bias",
    }
    if key in fixed:
        return fixed[key]

    # Encoder conv-Schichten unveraendert uebernehmen
    if re.match(r"encoder\.conv\d\.", key):
        return key

    # Encoder blocks
    m = re.match(r"encoder\.layers\.(\d+)\.(.+)", key)
    if m:
        idx, rest = m.group(1), m.group(2)
        mapped = _block_key(rest, cross=False)
        return f"encoder.blocks.{idx}.{mapped}" if mapped else None

    # Decoder blocks
    m = re.match(r"decoder\.layers\.(\d+)\.(.+)", key)
    if m:
        idx, rest = m.group(1), m.group(2)
        mapped = _block_key(rest, cross=True)
        return f"decoder.blocks.{idx}.{mapped}" if mapped else None

    return None  # Unbekannte / PEFT-spezifische Keys ignorieren


def _block_key(key: str, cross: bool) -> "str | None":
    """Mappt Block-Level Keys von HuggingFace auf mlx_whisper."""
    table = {
        "self_attn.q_proj.weight":     "attn.query.weight",
        "self_attn.q_proj.bias":       "attn.query.bias",
        "self_attn.k_proj.weight":     "attn.key.weight",
        "self_attn.v_proj.weight":     "attn.value.weight",
        "self_attn.v_proj.bias":       "attn.value.bias",
        "self_attn.out_proj.weight":   "attn.out.weight",
        "self_attn.out_proj.bias":     "attn.out.bias",
        "self_attn_layer_norm.weight": "attn_ln.weight",
        "self_attn_layer_norm.bias":   "attn_ln.bias",
        "fc1.weight":                  "mlp1.weight",
        "fc1.bias":                    "mlp1.bias",
        "fc2.weight":                  "mlp2.weight",
        "fc2.bias":                    "mlp2.bias",
        "final_layer_norm.weight":     "mlp_ln.weight",
        "final_layer_norm.bias":       "mlp_ln.bias",
    }
    if cross:
        table.update({
            "encoder_attn.q_proj.weight":     "cross_attn.query.weight",
            "encoder_attn.q_proj.bias":       "cross_attn.query.bias",
            "encoder_attn.k_proj.weight":     "cross_attn.key.weight",
            "encoder_attn.v_proj.weight":     "cross_attn.value.weight",
            "encoder_attn.v_proj.bias":       "cross_attn.value.bias",
            "encoder_attn.out_proj.weight":   "cross_attn.out.weight",
            "encoder_attn.out_proj.bias":     "cross_attn.out.bias",
            "encoder_attn_layer_norm.weight": "cross_attn_ln.weight",
            "encoder_attn_layer_norm.bias":   "cross_attn_ln.bias",
        })
    return table.get(key)


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
        import json as _json
        import torch
        import mlx.core as mx
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        # Modell laden – model.model (nicht model) ueberspringt den PEFT-Wrapper
        # und liefert Keys ohne fuehrendes "model." Praefix
        log.info("  [1/3] Lade Modell von HuggingFace...")
        hf_model = WhisperForConditionalGeneration.from_pretrained(CH_MODEL_HF)
        processor = WhisperProcessor.from_pretrained(CH_MODEL_HF)
        inner = hf_model.model  # WhisperModel (ohne PEFT-Wrapper)

        log.info("  [2/3] Konvertiere Gewichte (HF-Keys -> mlx_whisper-Keys)...")
        CH_MODEL_LOCAL.mkdir(parents=True, exist_ok=True)
        mlx_weights = {}
        skipped = 0
        for hf_key, tensor in inner.state_dict().items():
            mlx_key = _hf_to_mlx_key(hf_key)
            if mlx_key:
                # float16: Whisper-Standard fuer Inference (float32 wuerde
                # dtype-Mismatch im Decoder ausloesen)
                np_arr = tensor.cpu().to(torch.float16).numpy()
                # Conv1d: PyTorch [C_out, C_in, k] -> MLX [C_out, k, C_in]
                if hf_key in ("encoder.conv1.weight", "encoder.conv2.weight"):
                    np_arr = np_arr.transpose(0, 2, 1)
                mlx_weights[mlx_key] = mx.array(np_arr)
            else:
                skipped += 1
        log.info(f"    {len(mlx_weights)} Keys konvertiert, {skipped} uebersprungen.")
        mx.savez(str(CH_MODEL_LOCAL / "weights.npz"), **mlx_weights)

        log.info("  [3/3] Speichere Konfiguration und Tokenizer...")
        processor.save_pretrained(str(CH_MODEL_LOCAL))
        hf_cfg = hf_model.config.to_dict()
        mlx_cfg = {
            "n_mels":        hf_cfg.get("num_mel_bins", 80),
            "n_audio_ctx":   hf_cfg.get("max_source_positions", 1500),
            "n_audio_state": hf_cfg.get("d_model", 1024),
            "n_audio_head":  hf_cfg.get("encoder_attention_heads", 16),
            "n_audio_layer": hf_cfg.get("encoder_layers", 32),
            "n_vocab":       hf_cfg.get("vocab_size", 51866),
            "n_text_ctx":    hf_cfg.get("max_target_positions", 448),
            "n_text_state":  hf_cfg.get("d_model", 1024),
            "n_text_head":   hf_cfg.get("decoder_attention_heads", 16),
            "n_text_layer":  hf_cfg.get("decoder_layers", 32),
        }
        (CH_MODEL_LOCAL / "config.json").write_text(
            _json.dumps(mlx_cfg, indent=2), encoding="utf-8"
        )

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
            fp16=False,  # Unser konvertiertes CH-Modell liefert float32 → kein dtype-Mismatch
        )
        return result.get("text", "")

    text = retry_with_backoff(_transcribe, f"Transkription von {audio_path.name}")

    # Korrekturliste anwenden (Firmennamen, Fachbegriffe)
    if TRANSCRIPT_CORRECTIONS:
        for wrong, correct in TRANSCRIPT_CORRECTIONS.items():
            text = text.replace(wrong, correct)
        log.info(f"Korrekturen angewendet: {list(TRANSCRIPT_CORRECTIONS.keys())}")

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
            token=HF_TOKEN,
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
        "Du bist ein Protokollfuehrer in einem Schweizer Unternehmen.\n\n"
        "REGELN (nicht in den Output schreiben):\n"
        "- Erfinde NIEMALS Informationen. Nur was im Transkript steht.\n"
        "- Leere Felder: verwende exakt die unten angegebenen Platzhaltertexte.\n"
        "- Keine Erklaerungen, keine Beispiele, keine Anweisungen im Output.\n"
        "- Schweizer Rechtschreibung. Waehrungen in CHF.\n\n"
        "AUSGABEFORMAT:\n\n"
        "TITEL: [max. 8 Woerter, beschreibend]\n\n"
        "## Thema\n"
        "[Was im Transkript besprochen wurde. Falls unklar: Nicht eindeutig erkennbar.]\n\n"
        "## Teilnehmer\n"
        "[Namen aus dem Transkript. Falls keine: Nicht erwaehnt.]\n\n"
        "## Zusammenfassung\n"
        "[3-5 Saetze zu den besprochenen Punkten. Falls zu wenig Inhalt: "
        "Das Transkript enthaelt zu wenig Inhalt fuer eine Zusammenfassung.]\n\n"
        "## Entscheidungen\n"
        "[Beschlossene Punkte als Aufzaehlung. Falls keine: Keine Entscheidungen erwaehnt.]\n\n"
        "## Actionpoints\n"
        "[Format: - [ ] Person: Aufgabe bis Termin. Falls keine: Keine Actionpoints erwaehnt.]"
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

        # Schritt 4: Kurztitel aus Ollama-Antwort extrahieren
        title = base_name
        summary_body = summary
        for line in summary.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("TITEL:"):
                title = stripped.split(":", 1)[1].strip()
                summary_body = summary.replace(line, "", 1).lstrip("\n")
                break

        # Schritt 5: Protokoll als Markdown speichern
        today = date.today().isoformat()
        final_file = OUTPUT_DIR / f"{base_name}_Protokoll.md"
        final_file.write_text(
            f"# {today} – {title}\n\n"
            f"{summary_body}\n\n"
            "---\n\n"
            "## Detailliertes Transkript\n\n"
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
