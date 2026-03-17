
Hier ist dein kompletter, aufgeräumter **Master-Plan**. Du musst im Grunde nur noch diese Schritte von oben nach unten abarbeiten und die Codes kopieren. Alles ist detailliert kommentiert, damit du auch in 6 Monaten noch weisst, was das Skript da eigentlich macht.

---

### Schritt 1: Die Basis auf dem Mac vorbereiten

Öffne dein Terminal und führe diese Befehle nacheinander aus. Das richtet die Umgebung ein, die macOS-Updates überlebt.

```bash
# 1. Pyenv installieren (für die update-sichere Python-Version)
brew update
brew install pyenv

# 2. Python 3.11.7 installieren (sehr stabil für KI-Sachen)
pyenv install 3.11.7

# 3. Ollama Modell für die Zusammenfassung laden (Mistral NeMo)
ollama pull mistral-nemo
```

---

### Schritt 2: Die SSD einrichten

Ersetze in den folgenden Befehlen `ExtSSD` durch den echten Namen deiner Festplatte.

```bash
# 1. Auf die SSD wechseln und den Projektordner erstellen
mkdir -p /Volumes/ExtSSD/WhisperSystem
cd /Volumes/ExtSSD/WhisperSystem

# 2. Diese Ordner an die sichere Python-Version binden
pyenv local 3.11.7

# 3. Das unkaputtbare Virtual Environment (venv) erstellen und aktivieren
python -m venv venv
source venv/bin/activate

# 4. Alle nötigen KI-Pakete installieren
pip install insanely-fast-whisper watchdog requests
```

---

### Schritt 3: Das Python Master-Skript (`main.py`)

Erstelle auf der SSD im Ordner `WhisperSystem` eine Datei namens `main.py`. Kopiere diesen gesamten Code hinein. 

**Wichtig:** Trage ganz oben deinen Hugging Face Token und den korrekten Namen deiner SSD ein!

```python
import os
import time
import json
import subprocess
import requests
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ==========================================
# 1. KONFIGURATION & PFADE
# ==========================================
# WICHTIG: Passe den Namen "ExtSSD" an deinen echten Festplattennamen an!
SSD_PATH = "/Volumes/ExtSSD/WhisperSystem"
INPUT_DIR = os.path.join(SSD_PATH, "input")
OUTPUT_DIR = os.path.join(SSD_PATH, "output")
TEMP_DIR = os.path.join(SSD_PATH, "temp")

# WICHTIG: Hier deinen Hugging Face "Read"-Token eintragen!
HF_TOKEN = "hf_DEIN_TOKEN_HIER_EINTRAGEN" 

# Ordner automatisch erstellen, falls sie fehlen
for d in [INPUT_DIR, OUTPUT_DIR, TEMP_DIR]: 
    os.makedirs(d, exist_ok=True)

# ==========================================
# 2. LOGGING (Für die Fehlersuche)
# ==========================================
LOG_FILE = os.path.join(SSD_PATH, "system_log.txt")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'), # Schreibt ins Logfile auf der SSD
        logging.StreamHandler()                          # Schreibt zusätzlich ins Terminal
    ]
)

# ==========================================
# 3. KI-ZUSAMMENFASSUNG (OLLAMA)
# ==========================================
def summarize_with_ollama(text):
    logging.info("Sende Transkript an lokales Ollama-Modell (Mistral NeMo)...")
    
    # Der System-Prompt zwingt die KI in den Schweizer Business-Kontext
    system_instruction = (
        "Du bist ein hocheffizienter Protokollführer in einem Schweizer Unternehmen. "
        "Das folgende Transkript wurde aus dem Schweizerdeutschen ins Hochdeutsche übersetzt. "
        "Erstelle ein professionelles Protokoll mit folgenden Abschnitten:\n"
        "1. THEMA: Um was ging es primär?\n"
        "2. TEILNEHMER: Wer hat gesprochen?\n"
        "3. ZUSAMMENFASSUNG: Die wichtigsten Punkte in 3-5 Sätzen.\n"
        "4. ENTSCHEIDUNGEN: Was wurde beschlossen?\n"
        "5. ACTIONPOINTS: Wer muss was bis wann tun? (Klar aufgelistet)\n\n"
        "WICHTIG: Verwende Schweizer Rechtschreibung (kein ß, sondern ss). "
        "Währungen sind in CHF anzugeben."
    )
    
    try:
        r = requests.post("http://localhost:11434/api/generate", 
            json={
                "model": "mistral-nemo", # Das optimale Modell für 16GB RAM
                "system": system_instruction,
                "prompt": f"Hier ist das Transkript:\n\n{text}",
                "stream": False,
                "options": {
                    "num_ctx": 32768, # Grosses Gedächtnis für lange Meetings
                    "temperature": 0.3 # Niedrige Temperatur = sachlicher Text, keine Fantasie
                }
            })
        return r.json().get("response")
    except Exception as e: 
        logging.error(f"Ollama Fehler: {e}")
        return "FEHLER: Konnte keine Zusammenfassung erstellen. Läuft die Ollama App?"

# ==========================================
# 4. DER WÄCHTER (WATCHDOG)
# ==========================================
class AudioHandler(FileSystemEventHandler):
    def on_created(self, event):
        # Ignoriere Ordner, reagiere nur auf Audio-Dateien
        if event.is_directory or not event.src_path.lower().endswith(('.mp3', '.wav', '.m4a', '.aac')): 
            return
        
        # 3 Sekunden warten, damit der Mac die Datei komplett kopieren kann
        time.sleep(3) 
        filename = os.path.basename(event.src_path)
        base_name = os.path.splitext(filename)[0]
        json_out = os.path.join(TEMP_DIR, f"{base_name}.json")
        
        logging.info(f"Neue Datei erkannt: {filename}. Starte Verarbeitung...")
        
        # ------------------------------------------
        # 4a. Transkription & Sprechererkennung
        # ------------------------------------------
        # Der Befehl für insanely-fast-whisper. Hier steckt die M4-Magie.
        cmd = [
            "insanely-fast-whisper",
            "--file-path", event.src_path,
            "--device", "mps",           # Nutzt die Mac M4 GPU/NPU für extremen Speed
            "--language", "de",          # Übersetzt Schweizerdeutsch in Hochdeutsch
            "--hf-token", HF_TOKEN,      # Für die Sprechererkennung via Pyannote
            "--transcript-path", json_out,
            "--vad-filter", "true"       # ANTI-HALLUZINATION: Schneidet Stille und Rauschen weg
        ]
        
        try:
            # Führt den Befehl aus. check=True sorgt dafür, dass Fehler abgefangen werden
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            logging.info("Whisper & Pyannote erfolgreich beendet. Lese Daten...")
            
            # Lese die erzeugte JSON-Datei ein
            with open(json_out, "r", encoding="utf-8") as f: 
                data = json.load(f)
            
            # Formatiere den Text sauber mit Sprecher-Labels
            chunks = data.get("speakers", data.get("chunks", []))
            formatted_text = ""
            for c in chunks:
                spk = c.get("speaker", "Unbekannt").replace("SPEAKER_", "Person ")
                text = c.get('text', '').strip()
                formatted_text += f"{spk}: {text}\n\n"
            
            # ------------------------------------------
            # 4b. KI-Analyse anfordern
            # ------------------------------------------
            summary = summarize_with_ollama(formatted_text)
            
            # ------------------------------------------
            # 4c. Datei speichern
            # ------------------------------------------
            final_file = os.path.join(OUTPUT_DIR, f"{base_name}_Protokoll.txt")
            with open(final_file, "w", encoding="utf-8") as f:
                f.write("========================================\n")
                f.write("📊 KI-PROTOKOLL & ACTIONPOINTS\n")
                f.write("========================================\n\n")
                f.write(summary)
                f.write("\n\n\n========================================\n")
                f.write("🗣️ DETAILLIERTES TRANSKRIPT\n")
                f.write("========================================\n\n")
                f.write(formatted_text)
            
            logging.info(f"✅ Fertig! Protokoll gespeichert unter: {final_file}")
            
        except subprocess.CalledProcessError as e:
            logging.error(f"❌ Whisper Terminal-Fehler: {e.stderr}")
        except Exception as e: 
            logging.error(f"❌ Unerwarteter Fehler: {e}", exc_info=True)

# ==========================================
# 5. PROGRAMM-START
# ==========================================
if __name__ == "__main__":
    event_handler = AudioHandler()
    observer = Observer()
    observer.schedule(event_handler, INPUT_DIR, recursive=False)
    observer.start()
    
    print("\n" + "="*50)
    print(f"👁️  System aktiv! Überwache Ordner:")
    print(f"📂 {INPUT_DIR}")
    print("="*50 + "\n")
    logging.info("Wächter-Skript gestartet.")
    
    try:
        # Hält das Skript am Laufen
        while True: 
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nSystem wird beendet...")
    
    observer.join()
```

---

### Schritt 4: Der One-Click Start (Automator)

Damit du das Terminal nie wieder öffnen musst, baust du dir dein Programm-Icon:

1. Öffne **Automator** auf deinem Mac.
2. Wähle **Neues Dokument** -> **Programm**.
3. Suche in der Liste nach **"Shell-Skript ausführen"** und ziehe es in das rechte Feld.
4. Kopiere diesen Code dort hinein (Passe `ExtSSD` an!):

```bash
# Dieser absolute Pfad ist immun gegen macOS-Updates!
# Alle Fehlerausgaben werden in die startup_debug.log geschrieben, falls etwas klemmt.

/Volumes/ExtSSD/WhisperSystem/venv/bin/python3 /Volumes/ExtSSD/WhisperSystem/main.py >> /Volumes/ExtSSD/WhisperSystem/startup_debug.log 2>&1
```

5. Speichere das Automator-Programm z.B. als **"Meeting_Protokollant"** auf deinen Schreibtisch oder in dein Dock.

### Das war's!
Du hast jetzt eine absolut professionelle, lokale KI-Maschine gebaut. Ollama im Hintergrund laufen lassen, Automator-App anklicken, Audio einwerfen – und den Mac M4 die Magie machen lassen.

Gibt es noch ein Detail im Code, das dir unklar ist, oder möchtest du nun direkt loslegen und die Ordner anlegen?