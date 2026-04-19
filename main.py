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

from __future__ import annotations

import os
import re
import signal
import sys
import time
import json
import queue
import logging
import threading
import subprocess
import shutil
from dataclasses import dataclass, field
from datetime import date
from logging.handlers import RotatingFileHandler
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

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".mp4")
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # Sekunden


def _env_int(name: str, default: int) -> int:
    """Liest einen int-ENV-Wert, faellt bei Fehler auf default zurueck."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Pipeline: parallele Stages (Transkription waehrend vorherige Datei zusammengefasst wird)
PIPELINE_PARALLEL = os.getenv("PIPELINE_PARALLEL", "true").lower() == "true"
# Obergrenze, damit nicht beliebig viele Transkripte im RAM liegen
SUMMARIZE_QUEUE_MAX = _env_int("SUMMARIZE_QUEUE_MAX", 2)

# Retention: 0 = deaktiviert
ARCHIVE_RETENTION_DAYS = _env_int("ARCHIVE_RETENTION_DAYS", 90)
FAILED_RETENTION_DAYS = _env_int("FAILED_RETENTION_DAYS", 30)
RETENTION_SWEEP_HOURS = max(1, _env_int("RETENTION_SWEEP_HOURS", 24))

# Korrekturliste fuer haeufige Transkriptionsfehler bei Firmennamen / Fachbegriffen
# Format in .env: TRANSCRIPT_CORRECTIONS=Mantelux:Montalux,Strahlhorn:Strahlhorn AG
_raw_corrections = os.getenv("TRANSCRIPT_CORRECTIONS", "")
TRANSCRIPT_CORRECTIONS: dict[str, str] = {}
if _raw_corrections:
    for pair in _raw_corrections.split(","):
        if ":" in pair:
            wrong, correct = pair.split(":", 1)
            wrong = wrong.strip()
            correct = correct.strip()
            if wrong:  # Leere Keys ergaeben ein \b\b-Pattern, das ueberall matcht
                TRANSCRIPT_CORRECTIONS[wrong] = correct

# Pre-kompiliert mit Wortgrenzen, damit "Foo" nicht innerhalb von "Foobar" trifft
_CORRECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + re.escape(w) + r"\b"), c)
    for w, c in TRANSCRIPT_CORRECTIONS.items()
]

# Ordner erstellen
for d in [INPUT_DIR, OUTPUT_DIR, TEMP_DIR, FAILED_DIR, ARCHIVE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ==========================================
# 2. LOGGING (mit Rotation, damit das Log nicht unbegrenzt waechst)
# ==========================================
LOG_FILE = SSD_PATH / "system_log.txt"
_log_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_format)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_format)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger(__name__)

# Thread-safety fuer geteilte Zustaende (Watchdog + Worker laufen parallel)
_processed_lock = threading.Lock()
_diarization_lock = threading.Lock()

# ==========================================
# 3. HILFSFUNKTIONEN
# ==========================================

def load_processed_files():
    """Laedt die Liste bereits verarbeiteter Dateien."""
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            log.warning("processed_files.json korrupt, starte mit leerer Liste.")
    return set()


def save_processed_files(processed: set):
    """Speichert die Liste bereits verarbeiteter Dateien (thread-safe, atomar)."""
    with _processed_lock:
        tmp = PROCESSED_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(processed)), encoding="utf-8")
        tmp.replace(PROCESSED_FILE)


def _processing_marker(audio_path: Path) -> Path:
    """Marker-Datei neben der Input-Datei, die anzeigt dass eine Verarbeitung laeuft."""
    return audio_path.with_suffix(audio_path.suffix + ".processing")


def wait_for_stable_file(path: Path, interval: float = 2.0, checks: int = 3,
                         max_wait: float = 3600.0) -> bool:
    """
    Wartet bis eine Datei vollstaendig kopiert wurde.
    Gibt True zurueck, wenn die Datei stabil ist. False bei Timeout oder
    wenn die Datei dauerhaft bei 0 Bytes bleibt (abgebrochener Upload).
    """
    prev_size = -1
    stable_count = 0
    zero_start: float | None = None
    deadline = time.monotonic() + max_wait
    while stable_count < checks:
        if time.monotonic() > deadline:
            log.warning(f"Timeout beim Warten auf stabile Datei: {path.name}")
            return False
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        # Datei dauerhaft leer -> abgebrochener Upload, nicht weiter blockieren
        if size == 0:
            if zero_start is None:
                zero_start = time.monotonic()
            elif time.monotonic() - zero_start > 60:
                log.warning(f"Datei bleibt leer, breche ab: {path.name}")
                return False
            time.sleep(interval)
            continue
        zero_start = None
        if size == prev_size:
            stable_count += 1
        else:
            stable_count = 0
            prev_size = size
        if stable_count < checks:
            time.sleep(interval)
    log.info(f"Datei stabil: {path.name} ({prev_size / 1024 / 1024:.1f} MB)")
    return True


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


def _as_applescript_string(s: str) -> str:
    """Escape fuer AppleScript-String: Backslash und doppelte Anfuehrungszeichen."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify_macos(title: str, message: str):
    """Sendet eine macOS-Benachrichtigung (injection-sicher)."""
    try:
        safe_title = _as_applescript_string(title)
        safe_msg = _as_applescript_string(message)
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}"'],
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
    if (CH_MODEL_LOCAL / "weights.npz").exists() and (CH_MODEL_LOCAL / "config.json").exists():
        log.info(f"CH-Modell bereits konvertiert: {CH_MODEL_LOCAL}")
        return True

    log.info(f"Schweizerdeutsch-Modell wird erstmalig konvertiert...")
    log.info(f"  Quelle:  {CH_MODEL_HF} (HuggingFace, PyTorch)")
    log.info(f"  Ziel:    {CH_MODEL_LOCAL} (lokal, MLX)")
    log.info("  Dies dauert ca. 3-5 Minuten und braucht einmalig Internet.")

    try:
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
            json.dumps(mlx_cfg, indent=2), encoding="utf-8"
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
    if (CH_MODEL_LOCAL / "weights.npz").exists() and (CH_MODEL_LOCAL / "config.json").exists():
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
        # Exakter Match oder mit :tag-Suffix (z.B. "mistral-nemo" vs "mistral-nemo:latest")
        def _matches(m: str) -> bool:
            return m == OLLAMA_MODEL or m.startswith(OLLAMA_MODEL + ":")
        if not any(_matches(m) for m in models):
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
# 6. TRANSKRIPTION (MLX WHISPER)
# ==========================================

def _apply_corrections(text: str) -> str:
    """Wendet die Korrekturliste mit Wortgrenzen an."""
    for pattern, correct in _CORRECTION_PATTERNS:
        text = pattern.sub(correct, text)
    return text


def transcribe_audio(audio_path: Path) -> dict:
    """
    Transkribiert eine Audiodatei mit MLX Whisper.
    Gibt ein Dict zurueck mit 'text' und 'segments' (fuer Sprecher-Zuordnung).
    Kein retry_with_backoff: Whisper-Transkription ist deterministisch und
    teuer – bei einem Fehler wuerde jeder Versuch die ganze Datei neu rechnen.
    """
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=WHISPER_MODEL,
        language="de",
        word_timestamps=True,
        fp16=False,  # Unser konvertiertes CH-Modell liefert float32 → kein dtype-Mismatch
    )

    text = result.get("text", "")
    segments = result.get("segments", []) or []

    if _CORRECTION_PATTERNS:
        text = _apply_corrections(text)
        for seg in segments:
            if "text" in seg:
                seg["text"] = _apply_corrections(seg["text"])
        log.info(f"Korrekturen angewendet: {list(TRANSCRIPT_CORRECTIONS.keys())}")

    log.info(f"Transkription abgeschlossen: {len(text)} Zeichen, {len(segments)} Segmente")
    return {"text": text, "segments": segments}


# ==========================================
# 7. SPRECHERERKENNUNG (PYANNOTE)
# ==========================================

_diarization_pipeline = None


def get_diarization_pipeline():
    """Laedt die Pyannote-Pipeline (einmalig, thread-safe, auf MPS wenn moeglich)."""
    global _diarization_pipeline
    with _diarization_lock:
        if _diarization_pipeline is None:
            from pyannote.audio import Pipeline
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=HF_TOKEN,
            )
            # Auf Apple Silicon GPU verschieben (5-10x schneller als CPU)
            try:
                import torch
                if torch.backends.mps.is_available():
                    pipeline.to(torch.device("mps"))
                    log.info("Diarization-Pipeline auf MPS (Apple GPU).")
                else:
                    log.info("Diarization-Pipeline auf CPU (MPS nicht verfuegbar).")
            except Exception as e:
                log.warning(f"MPS-Transfer fehlgeschlagen, nutze CPU: {e}")
            _diarization_pipeline = pipeline
        return _diarization_pipeline


def _audio_duration(audio_path: Path) -> float | None:
    """Dauer der Audiodatei in Sekunden via ffprobe, oder None."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def diarize_audio(audio_path: Path) -> list:
    """
    Erkennt Sprecher in einer Audiodatei.
    Gibt eine Liste von (start, end, speaker) Tupeln zurueck.
    Pyannote benoetigt mindestens ~12 Sekunden Audio (chunk-Grenze).
    """
    duration = _audio_duration(audio_path)
    if duration is not None and duration < 12.0:
        log.warning(
            f"Sprechererkennung uebersprungen: Datei zu kurz ({duration:.1f}s, "
            f"Minimum: 12s). Fahre ohne Sprecher-Labels fort."
        )
        return []

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


def _speaker_for_segment(seg_start: float, seg_end: float,
                         diar_segments: list) -> "str | None":
    """Findet den Sprecher mit groesstem zeitlichen Overlap zu einem Whisper-Segment."""
    best_speaker = None
    best_overlap = 0.0
    for ds, de, sp in diar_segments:
        overlap = max(0.0, min(de, seg_end) - max(ds, seg_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = sp
    return best_speaker


def merge_transcript_with_speakers(whisper_segments: list, diar_segments: list) -> str:
    """
    Kombiniert Whisper-Segmente mit Sprecher-Labels aus Pyannote via Zeit-Overlap.
    Fasst aufeinanderfolgende Segmente desselben Sprechers zu einem Absatz zusammen.
    """
    if not diar_segments or not whisper_segments:
        return ""

    # Sprecher stabil nach erstem Auftreten nummerieren: SPEAKER_00 -> Person 1
    speaker_map: dict[str, str] = {}
    counter = 1
    for _, _, sp in diar_segments:
        if sp not in speaker_map:
            speaker_map[sp] = f"Person {counter}"
            counter += 1

    lines: list[str] = []
    current_label: str | None = None
    buffer: list[str] = []

    def _flush():
        if current_label and buffer:
            lines.append(f"{current_label}: {' '.join(t.strip() for t in buffer).strip()}")

    for seg in whisper_segments:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        seg_text = seg.get("text", "").strip()
        if not seg_text:
            continue
        sp = _speaker_for_segment(start, end, diar_segments)
        label = speaker_map.get(sp, "Unbekannt") if sp else "Unbekannt"
        if label != current_label:
            _flush()
            buffer = []
            current_label = label
        buffer.append(seg_text)
    _flush()

    header = f"Erkannte Sprecher: {', '.join(speaker_map.values())}\n\n"
    return header + "\n\n".join(lines)


# ==========================================
# 8. KI-ZUSAMMENFASSUNG (OLLAMA)
# ==========================================

def _chunk_text(text: str, max_chars: int = 25000) -> list[str]:
    """Teilt Text in Chunks auf, bevorzugt an Absatzgrenzen."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        # Suche letzten Absatzumbruch vor dem Limit
        split_at = remaining.rfind("\n\n", 0, max_chars)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    log.info(f"Text in {len(chunks)} Chunks aufgeteilt ({len(text)} Zeichen gesamt)")
    return chunks


def _pick_num_ctx(total_chars: int) -> int:
    """
    Kontextfenster dynamisch an Input-Groesse anpassen.
    Heuristik: ~3 Chars/Token im Deutschen + Puffer fuer System-Prompt + Output.
    Stufen: 4k / 8k / 16k / 32k – groesser waere fuer Mistral-Nemo zu teuer.
    """
    est_tokens = total_chars // 3 + 1024  # Puffer fuer System + erwartete Antwort
    for step in (4096, 8192, 16384, 32768):
        if est_tokens <= step:
            return step
    return 32768


def _call_ollama(system: str, prompt: str, timeout: int = 600,
                 num_ctx: int | None = None) -> str:
    """Einzelner Ollama-API-Aufruf mit konfigurierbarem Timeout und Kontext."""
    if num_ctx is None:
        num_ctx = _pick_num_ctx(len(system) + len(prompt))
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_ctx": num_ctx,
                "temperature": 0.3,
            },
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def summarize_with_ollama(text: str) -> str:
    """Erstellt ein strukturiertes Protokoll mit Ollama."""
    log.info(f"Sende Transkript an Ollama ({OLLAMA_MODEL})... ({len(text)} Zeichen)")

    # Dynamischer Timeout: 600s Basis + 120s pro 10'000 Zeichen
    timeout = 600 + (len(text) // 10000) * 120
    log.info(f"Ollama-Timeout: {timeout}s")

    protocol_instruction = (
        "Du bist ein Protokollfuehrer in einem Schweizer Unternehmen.\n"
        "Erstelle ein strukturiertes Meeting-Protokoll basierend auf dem Transkript.\n\n"
        "PFLICHTREGELN:\n"
        "1. Erfinde NIEMALS Informationen. Schreibe nur was im Transkript explizit vorkommt.\n"
        "2. Keine Erklaerungen, keine Meta-Kommentare, keine Anweisungen im Output.\n"
        "3. Schweizer Rechtschreibung: 'ss' statt 'ß'. Waehrungen in CHF.\n"
        "4. Gibt es zu einer Rubrik keine Information im Transkript, verwende den "
        "vorgegebenen Standardtext.\n\n"
        "AUSGABE – genau dieses Format, nichts anderes:\n\n"
        "TITEL: <beschreibender Titel, max. 8 Woerter>\n\n"
        "## Thema\n"
        "<Zusammenfassung des Hauptthemas aus dem Transkript>\n"
        "STANDARDTEXT FALLS KEIN THEMA: Nicht eindeutig erkennbar.\n\n"
        "## Teilnehmer\n"
        "<Kommagetrennte Namen der Teilnehmer aus dem Transkript>\n"
        "STANDARDTEXT FALLS KEINE NAMEN: Nicht erwaehnt.\n\n"
        "## Zusammenfassung\n"
        "<3 bis 5 Saetze ueber die besprochenen Inhalte>\n"
        "STANDARDTEXT FALLS ZU WENIG INHALT: "
        "Das Transkript enthaelt zu wenig Inhalt fuer eine Zusammenfassung.\n\n"
        "## Entscheidungen\n"
        "<Getroffene Entscheidungen als Stichpunkte>\n"
        "STANDARDTEXT FALLS KEINE: Keine Entscheidungen erwaehnt.\n\n"
        "## Actionpoints\n"
        "<Aufgaben im Format: - [ ] Person: Aufgabe bis Datum>\n"
        "STANDARDTEXT FALLS KEINE: Keine Actionpoints erwaehnt."
    )

    chunks = _chunk_text(text)

    if len(chunks) == 1:
        # Kurzer Text: direkt zusammenfassen
        def _summarize():
            return _call_ollama(protocol_instruction,
                                f"Hier ist das Transkript:\n\n{text}",
                                timeout=timeout)
        summary = retry_with_backoff(_summarize, "Ollama-Zusammenfassung")
    else:
        # Langer Text: Chunks einzeln zusammenfassen, dann kombinieren
        chunk_summaries = []
        for i, chunk in enumerate(chunks, 1):
            log.info(f"Zusammenfassung Chunk {i}/{len(chunks)}...")
            chunk_system = (
                "Du bist ein Protokollfuehrer. Fasse den folgenden Teil eines "
                "Meeting-Transkripts zusammen. Nenne alle wichtigen Punkte, "
                "Entscheidungen, Teilnehmer und Aufgaben. "
                "Schweizer Rechtschreibung (kein 'ß', benutze 'ss')."
            )

            def _summarize_chunk(c=chunk):
                return _call_ollama(chunk_system,
                                    f"Transkript-Teil {i}/{len(chunks)}:\n\n{c}",
                                    timeout=timeout)

            chunk_summary = retry_with_backoff(_summarize_chunk,
                                               f"Ollama-Chunk {i}/{len(chunks)}")
            chunk_summaries.append(chunk_summary)

        # Chunk-Zusammenfassungen zum finalen Protokoll kombinieren
        combined = "\n\n---\n\n".join(chunk_summaries)
        log.info(f"Kombiniere {len(chunks)} Chunk-Zusammenfassungen zum Protokoll...")

        def _finalize():
            return _call_ollama(
                protocol_instruction,
                f"Hier sind die Zusammenfassungen der einzelnen Meeting-Teile. "
                f"Erstelle daraus EIN zusammenhaengendes Protokoll:\n\n{combined}",
                timeout=timeout,
            )
        summary = retry_with_backoff(_finalize, "Ollama-Finale-Zusammenfassung")

    log.info(f"Zusammenfassung erstellt: {len(summary)} Zeichen")
    return summary


# ==========================================
# 9. PIPELINE-STAGES (TRANSKRIBIEREN || ZUSAMMENFASSEN)
# ==========================================
#
# Zwei-Stufen-Pipeline: Waehrend Stage 2 (Ollama) fuer Datei N laeuft,
# transkribiert Stage 1 (Whisper + Diarization) bereits Datei N+1.
# Das bringt ~30-40 % Throughput-Gewinn, weil Whisper und Ollama zwar
# beide die GPU nutzen aber unterschiedlich belasten (Whisper streamt
# Audio ein, Ollama ist tokenweise), und die Input-Vorbereitung
# (wait_for_stable_file) nie mehr die Pipeline blockiert.
#
# Die summarize_queue begrenzt auf SUMMARIZE_QUEUE_MAX (default 2),
# damit nicht beliebig viele Transkripte im RAM liegen bleiben
# wenn Ollama langsamer ist als Whisper.


@dataclass
class WorkItem:
    """Zustand, der zwischen den Pipeline-Stages weitergereicht wird."""
    audio_path: Path
    filename: str
    base_name: str
    marker: Path
    text: str = ""
    segments: list = field(default_factory=list)
    speaker_block: str = ""
    error: "Exception | None" = None


def _move_to_failed(audio_path: Path) -> None:
    """Verschiebt eine Datei nach failed/ (mit Timestamp bei Namenskonflikt)."""
    if not audio_path.exists():
        return
    try:
        dest = FAILED_DIR / audio_path.name
        if dest.exists():
            dest = FAILED_DIR / f"{audio_path.stem}_{int(time.time())}{audio_path.suffix}"
        shutil.move(str(audio_path), str(dest))
        log.info(f"Datei verschoben nach: {dest}")
    except Exception as e:
        log.error(f"Konnte {audio_path.name} nicht nach failed/ verschieben: {e}")


def _archive_input(audio_path: Path) -> None:
    """Verschiebt eine erfolgreich verarbeitete Datei ins Archiv."""
    dest = ARCHIVE_DIR / audio_path.name
    if dest.exists():
        dest = ARCHIVE_DIR / f"{audio_path.stem}_{int(time.time())}{audio_path.suffix}"
    shutil.move(str(audio_path), str(dest))


def _cleanup_marker(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except Exception:
        pass


def _extract_title(summary: str, default: str) -> tuple[str, str]:
    """Zieht 'TITEL: xy' aus der Ollama-Antwort; liefert (title, body_ohne_titel)."""
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("TITEL:"):
            title = stripped.split(":", 1)[1].strip() or default
            body = summary.replace(line, "", 1).lstrip("\n")
            return title, body
    return default, summary


def _discard_in_flight(in_flight: set, lock: threading.Lock, filename: str) -> None:
    """Entfernt eine Datei aus dem Dedup-Set (nach Abschluss oder Fehler)."""
    with lock:
        in_flight.discard(filename)


def _put_with_shutdown(q: "queue.Queue", item, stop_event: threading.Event,
                       timeout: float = 1.0) -> bool:
    """
    Put mit Shutdown-Abbruch: re-tried bis queue Platz hat ODER stop_event gesetzt ist.
    Gibt False zurueck, wenn wegen Shutdown nicht zugestellt werden konnte.
    """
    while not stop_event.is_set():
        try:
            q.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue
    return False


def stage_transcribe(
    input_queue: "queue.Queue[Path | None]",
    summarize_queue: "queue.Queue[WorkItem | None]",
    processed: set,
    in_flight: set,
    in_flight_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """
    Stage 1: Wartet auf stabile Datei, transkribiert, diarisiert.
    Reicht fertiges WorkItem an Stage 2 weiter.
    """
    while True:
        try:
            path = input_queue.get(timeout=1.0)
        except queue.Empty:
            if stop_event.is_set():
                break
            continue

        if path is None:  # Shutdown-Sentinel
            input_queue.task_done()
            break

        filename = path.name
        base_name = path.stem

        try:
            # Bereits verarbeitet? Dann ueberspringen und Dedup-Set bereinigen.
            if filename in processed:
                log.info(f"Ueberspringe bereits verarbeitete Datei: {filename}")
                _discard_in_flight(in_flight, in_flight_lock, filename)
                continue

            if not path.exists():
                log.info(f"Datei verschwunden, ueberspringe: {filename}")
                _discard_in_flight(in_flight, in_flight_lock, filename)
                continue

            if not wait_for_stable_file(path):
                log.warning(f"Datei nicht stabil, verschiebe nach failed/: {filename}")
                _move_to_failed(path)
                _discard_in_flight(in_flight, in_flight_lock, filename)
                continue

            log.info(f"=== Transkription: {filename} ===")
            marker = _processing_marker(path)
            try:
                marker.touch(exist_ok=True)
            except Exception:
                pass

            item = WorkItem(
                audio_path=path, filename=filename, base_name=base_name, marker=marker
            )

            try:
                transcript = transcribe_audio(path)
                item.text = transcript["text"]
                item.segments = transcript["segments"]
            except Exception as e:
                log.exception(f"Transkription fehlgeschlagen: {filename}")
                item.error = e
                _put_with_shutdown(summarize_queue, item, stop_event)
                continue

            if not item.text.strip():
                log.warning(f"Leeres Transkript fuer {filename}.")
                item.error = RuntimeError("Leeres Transkript")
                _put_with_shutdown(summarize_queue, item, stop_event)
                continue

            if ENABLE_DIARIZATION and HF_TOKEN:
                try:
                    diar = diarize_audio(path)
                    merged = merge_transcript_with_speakers(item.segments, diar)
                    if merged:
                        item.speaker_block = merged
                except Exception as e:
                    log.warning(f"Sprechererkennung fehlgeschlagen, fahre ohne fort: {e}")

            if not _put_with_shutdown(summarize_queue, item, stop_event):
                # Shutdown waehrend wir auf Kapazitaet warten -> lokal aufraeumen
                _cleanup_marker(item.marker)
                _discard_in_flight(in_flight, in_flight_lock, filename)
        except Exception:
            log.exception(f"Unerwarteter Fehler in Transcribe-Stage bei {filename}")
            _discard_in_flight(in_flight, in_flight_lock, filename)
        finally:
            input_queue.task_done()

    # Stage 2 herunterfahren (best-effort, max. 5s warten)
    try:
        summarize_queue.put(None, timeout=5)
    except queue.Full:
        pass


def stage_summarize(
    summarize_queue: "queue.Queue[WorkItem | None]",
    processed: set,
    in_flight: set,
    in_flight_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """
    Stage 2: Erzeugt Protokoll via Ollama, schreibt Markdown,
    archiviert Input, markiert als verarbeitet.
    """
    while True:
        try:
            item = summarize_queue.get(timeout=1.0)
        except queue.Empty:
            if stop_event.is_set():
                break
            continue

        if item is None:
            summarize_queue.task_done()
            break

        try:
            # Fehler aus Stage 1 -> Datei nach failed/, sauber aufraeumen
            if item.error is not None:
                log.error(f"Kein Protokoll fuer {item.filename}: {item.error}")
                _move_to_failed(item.audio_path)
                notify_macos("Meeting Protokollant", f"Fehler bei: {item.base_name}")
                continue

            try:
                summary = summarize_with_ollama(item.text)
                title, summary_body = _extract_title(summary, item.base_name)

                today = date.today().isoformat()
                final_file = OUTPUT_DIR / f"{item.base_name}_Protokoll.md"
                transcript_section = item.speaker_block or item.text
                final_file.write_text(
                    f"# {today} – {title}\n\n"
                    f"{summary_body}\n\n"
                    "---\n\n"
                    "## Detailliertes Transkript\n\n"
                    f"{transcript_section}\n",
                    encoding="utf-8",
                )

                _archive_input(item.audio_path)

                for temp_file in TEMP_DIR.glob(f"{item.base_name}.*"):
                    temp_file.unlink(missing_ok=True)

                with _processed_lock:
                    processed.add(item.filename)
                save_processed_files(processed)

                log.info(f"Fertig! Protokoll: {final_file}")
                notify_macos("Meeting Protokollant", f"Protokoll fertig: {item.base_name}")
            except Exception:
                log.exception(f"Fehler bei Zusammenfassung/Write fuer {item.filename}")
                _move_to_failed(item.audio_path)
                notify_macos("Meeting Protokollant", f"Fehler bei: {item.base_name}")
        finally:
            _cleanup_marker(item.marker)
            _discard_in_flight(in_flight, in_flight_lock, item.filename)
            summarize_queue.task_done()


# ==========================================
# 10. WATCHDOG (EVENTS -> INPUT-QUEUE)
# ==========================================

class AudioHandler(FileSystemEventHandler):
    """
    Legt neue Dateien in die input_queue. Der Watchdog-Thread darf NICHT
    in on_created blockieren, sonst gehen parallele Events verloren.
    Dedup ueber gemeinsamen in_flight-Set mit den Workern.
    """
    def __init__(
        self,
        input_queue: "queue.Queue[Path | None]",
        in_flight: set,
        in_flight_lock: threading.Lock,
    ):
        self.queue = input_queue
        self.in_flight = in_flight
        self.lock = in_flight_lock

    def _handle(self, path: Path):
        # Marker-/Temp-/versteckte Dateien ignorieren
        if path.name.startswith(".") or path.suffix == ".processing":
            return
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return
        with self.lock:
            if path.name in self.in_flight:
                return
            self.in_flight.add(path.name)
        log.info(f"Neue Datei erkannt: {path.name} -> Queue")
        self.queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event):
        # Drag&Drop aus Finder erzeugt teilweise on_moved statt on_created
        if not event.is_directory:
            self._handle(Path(event.dest_path))


# ==========================================
# 11. JANITOR (RETENTION FUER archive/ UND failed/)
# ==========================================

def _sweep_directory(directory: Path, retention_days: int, label: str) -> None:
    """Loescht Dateien in directory die aelter als retention_days sind."""
    if retention_days <= 0 or not directory.exists():
        return
    threshold = time.time() - retention_days * 86400
    deleted = 0
    freed = 0
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime >= threshold:
            continue
        try:
            entry.unlink()
            deleted += 1
            freed += stat.st_size
        except Exception as e:
            log.warning(f"Retention {label}: konnte {entry.name} nicht loeschen: {e}")
    if deleted:
        log.info(
            f"Retention {label}: {deleted} Datei(en) geloescht "
            f"({freed / 1024 / 1024:.1f} MB, > {retention_days} Tage)"
        )


def janitor_worker(stop_event: threading.Event) -> None:
    """Raeumt periodisch alte Dateien aus archive/ und failed/ auf."""
    if ARCHIVE_RETENTION_DAYS <= 0 and FAILED_RETENTION_DAYS <= 0:
        log.info("Retention deaktiviert (beide Werte <= 0).")
        return
    sweep_interval = RETENTION_SWEEP_HOURS * 3600
    while not stop_event.is_set():
        try:
            _sweep_directory(ARCHIVE_DIR, ARCHIVE_RETENTION_DAYS, "archive")
            _sweep_directory(FAILED_DIR, FAILED_RETENTION_DAYS, "failed")
        except Exception:
            log.exception("Janitor-Fehler")
        # Wait-with-exit: pruefe jede Sekunde auf stop, damit shutdown schnell reagiert
        waited = 0
        while waited < sweep_interval and not stop_event.is_set():
            time.sleep(1)
            waited += 1


# ==========================================
# 12. STARTUP-SCAN
# ==========================================

def scan_existing_files(
    input_queue: "queue.Queue[Path | None]",
    in_flight: set,
    in_flight_lock: threading.Lock,
) -> None:
    """
    Legt Audiodateien aus dem Input-Ordner in die Queue.
    Crashte eine vorherige Verarbeitung (erkennbar am .processing-Marker),
    wird die Datei in /failed verschoben, damit sie nicht in Endlosschleife
    wiederholt wird.
    """
    existing = sorted(
        f for f in INPUT_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not existing:
        log.info("Keine bestehenden Dateien im Input-Ordner.")
        return

    log.info(f"Startup-Scan: {len(existing)} Audiodatei(en) gefunden.")
    for audio_file in existing:
        marker = _processing_marker(audio_file)
        if marker.exists():
            log.warning(
                f"Abgebrochene Verarbeitung erkannt ({audio_file.name}), "
                f"verschiebe nach failed/ zur manuellen Pruefung."
            )
            _move_to_failed(audio_file)
            _cleanup_marker(marker)
            continue
        with in_flight_lock:
            if audio_file.name in in_flight:
                continue
            in_flight.add(audio_file.name)
        input_queue.put(audio_file)


# ==========================================
# 13. HAUPTPROGRAMM
# ==========================================

def main():
    print("\n" + "=" * 50)
    print("  Meeting Protokollant -- Systemstart")
    print("=" * 50)

    # Konfiguration anzeigen
    log.info(f"SSD-Pfad:           {SSD_PATH}")
    log.info(f"Ollama-Modell:      {OLLAMA_MODEL}")
    log.info(f"Sprechererkennung:  {'Aktiviert' if ENABLE_DIARIZATION else 'Deaktiviert'}")
    log.info(f"Pipeline-Parallel:  {'Aktiviert' if PIPELINE_PARALLEL else 'Deaktiviert'}")
    log.info(
        f"Retention:          archive={ARCHIVE_RETENTION_DAYS}d, "
        f"failed={FAILED_RETENTION_DAYS}d, sweep={RETENTION_SWEEP_HOURS}h"
    )

    # Modell-Auswahl (ggf. mit Auto-Konvertierung)
    print("\n--- Modell-Setup ---")
    resolve_whisper_model()
    log.info(f"Whisper-Modell:     {WHISPER_MODEL}")

    # Health-Checks
    print("\n--- Health-Checks ---")
    whisper_ok = check_whisper()
    ollama_ok = check_ollama()
    diarization_ok = check_diarization()  # noqa: F841 (informativ)

    if not whisper_ok:
        log.error("MLX Whisper nicht verfuegbar. Abbruch.")
        sys.exit(1)
    if not ollama_ok:
        log.warning("Ollama nicht verfuegbar. Zusammenfassungen werden fehlschlagen.")

    # Verarbeitungsliste + geteilter Zustand
    processed = load_processed_files()
    log.info(f"Bereits verarbeitet: {len(processed)} Datei(en)")
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()

    # Queues + Worker-Threads
    input_queue: "queue.Queue[Path | None]" = queue.Queue()
    # Bei serieller Pipeline muss die summarize_queue sequentiell bleiben
    # (sonst werden die Garantien des Backpressure-Verhaltens umgangen).
    summarize_queue: "queue.Queue[WorkItem | None]" = queue.Queue(
        maxsize=SUMMARIZE_QUEUE_MAX if PIPELINE_PARALLEL else 1
    )
    stop_event = threading.Event()

    transcribe_thread = threading.Thread(
        target=stage_transcribe,
        args=(input_queue, summarize_queue, processed, in_flight, in_flight_lock, stop_event),
        name="TranscribeStage",
        daemon=True,
    )
    summarize_thread = threading.Thread(
        target=stage_summarize,
        args=(summarize_queue, processed, in_flight, in_flight_lock, stop_event),
        name="SummarizeStage",
        daemon=True,
    )
    janitor_thread = threading.Thread(
        target=janitor_worker,
        args=(stop_event,),
        name="Janitor",
        daemon=True,
    )

    transcribe_thread.start()
    summarize_thread.start()
    janitor_thread.start()

    # SIGTERM (launchd, kill, Shutdown-Trigger) wie KeyboardInterrupt behandeln
    def _signal_handler(signum, _frame):
        log.info(f"Signal {signum} empfangen, starte Shutdown...")
        stop_event.set()
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        signal.signal(signal.SIGHUP, _signal_handler)
    except (AttributeError, ValueError):
        pass  # SIGHUP auf Windows nicht verfuegbar

    # Startup-Scan (fuellt die input_queue)
    print("\n--- Startup-Scan ---")
    scan_existing_files(input_queue, in_flight, in_flight_lock)

    # Watchdog starten
    event_handler = AudioHandler(input_queue, in_flight, in_flight_lock)
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
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nSystem wird beendet...")
        log.info("System manuell gestoppt.")
    finally:
        stop_event.set()
        observer.stop()
        # Sentinels schicken, damit die Stages aus blockierenden get() rausfallen
        try:
            input_queue.put(None, timeout=1)
        except queue.Full:
            pass
        observer.join(timeout=10)
        transcribe_thread.join(timeout=30)
        # stage_transcribe schickt selbst ein None an summarize_queue beim Beenden.
        # Falls die Stage haengt, zur Sicherheit noch eins hinterherschicken.
        if summarize_thread.is_alive():
            try:
                summarize_queue.put(None, timeout=1)
            except queue.Full:
                pass
        summarize_thread.join(timeout=30)
        janitor_thread.join(timeout=5)
        log.info("Shutdown abgeschlossen.")


if __name__ == "__main__":
    main()
