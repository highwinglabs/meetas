"""Server-side i18n for REST error ``detail`` and the consent text.

The API is German by default. A client may request English with the
``Accept-Language`` header (e.g. ``en``); anything else (including an absent
header) falls back to German, so existing clients and tests are unaffected.

Only the *surface* that reaches the API is localised here: the exception
messages that the ``_handle`` funnel in :mod:`core.api.rest` turns into HTTP
errors, plus the consent text. Stored records, logs and wire values are never
translated -- only the human-readable ``detail`` sent to a specific client.

Matching is done on the already-produced German message:
  * static messages are looked up in a dict (exact match);
  * messages with dynamic parts use a small set of compiled patterns whose
    ``{0}``/``{1}`` placeholders capture the runtime value and refill the
    English template.
An unknown message is returned unchanged (German), which is always a safe
fallback.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

SUPPORTED_LANGS = ("de", "en")
DEFAULT_LANGUAGE = "de"

# The language for the current request. Set per-request by the ASGI middleware
# in core.api.app; defaults to German so background code and tests see German.
current_language: ContextVar[str] = ContextVar("ma_i18n_language", default=DEFAULT_LANGUAGE)


def parse_language(header: str | None) -> str:
    """Map an ``Accept-Language`` header to one of SUPPORTED_LANGS.

    Honours q-values and picks the highest-quality supported tag; an absent or
    unrecognised header yields the German default."""
    if not header:
        return DEFAULT_LANGUAGE
    best: str | None = None
    best_q = -1.0
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        q = 1.0
        if ";" in part:
            lang, _, qpart = part.partition(";")
            for tok in qpart.split(";"):
                tok = tok.strip()
                if tok.lower().startswith("q="):
                    try:
                        q = float(tok[2:])
                    except ValueError:
                        q = 1.0
        else:
            lang = part
        lang = lang.strip().lower()
        if not lang or lang == "*":
            continue
        base = lang.split("-")[0]
        if base in SUPPORTED_LANGS and q > best_q:
            best, best_q = base, q
    return best or DEFAULT_LANGUAGE


def consent_text_for_language(lang: str) -> str:
    """Default consent notice for a language (German fallback)."""
    return DEFAULT_CONSENT_EN if lang == "en" else DEFAULT_CONSENT_DE


DEFAULT_CONSENT_DE = (
    "Diese Anwendung nimmt Tonaufnahmen auf, speichert sie lokal und "
    "verarbeitet sie zu Transkripten und Analysen. Alle Daten bleiben "
    "standardmäßig auf diesem Gerät. Es erfolgt keine Übertragung in "
    "die Cloud."
)
DEFAULT_CONSENT_EN = (
    "This application records audio, stores it locally, and processes it into "
    "transcripts and analyses. All data stays on this device by default. "
    "Nothing is transferred to the cloud."
)


# (german template, english template). ``{0}``, ``{1}``, ... mark dynamic parts
# in both templates. Order matters only insofar as the first matching dynamic
# pattern wins; static messages are resolved by exact dict lookup first.
_ERROR_CATALOG: list[tuple[str, str]] = [
    ("'Externe Anbieter erlauben' aktivieren Sie ausdrücklich mit confirm_network_allowed=true. Dadurch werden nicht-lokale LLM-Endpunkte erlaubt.",
     "You explicitly enabled 'Allow external providers' with confirm_network_allowed=true. This allows non-local LLM endpoints."),
    ("'current' und 'new_name' dürfen nicht leer sein.",
     "'current' and 'new_name' must not be empty."),
    ("Aufgabe nicht gefunden: {0}", "Task not found: {0}"),
    ("Objekt mit der ID {0} nicht gefunden.", "Object with ID {0} not found."),
    ("Service nicht initialisiert", "Service not initialized"),
    ("Upload ist zu groß (max. {0} MiB direkt).", "Upload is too large (max. {0} MiB direct)."),
    ("Chunk ist zu groß (max. {0} MiB pro Chunk).", "Chunk is too large (max. {0} MiB per chunk)."),
    ("X-Upload-Offset fehlt oder ist ungültig.", "X-Upload-Offset is missing or invalid."),
    ("ASR lieferte ein ungültiges Transkriptsegment.", "ASR produced an invalid transcript segment."),
    ("ASR lieferte ungültige Zeitdaten.", "ASR produced invalid timing data."),
    ("ASR-Modell '{0}' ist nicht heruntergeladen. Starten Sie den Download zuerst (Netzwerk aktivieren + ausdrücklich bestätigen). {1}",
     "ASR model '{0}' is not downloaded. Start the download first (enable network + explicitly confirm). {1}"),
    ("ASR-Modell '{0}' ist nicht vorhanden.", "ASR model '{0}' is not available."),
    ("Aktive Aufnahmen können nicht gelöscht werden.", "Active recordings cannot be deleted."),
    ("Audio konnte nicht aus dem Upload gelesen werden: {0}", "Audio could not be read from the upload: {0}"),
    ("Audio konnte nicht geladen werden: {0}", "Audio could not be loaded: {0}"),
    ("Audio-Datei konnte für Parakeet nicht gelesen werden: {0}", "Audio file could not be read for Parakeet: {0}"),
    ("Audio-Datei nicht gefunden: {0}", "Audio file not found: {0}"),
    ("Audioverarbeitung dauerte zu lange und wurde beendet.", "Audio processing took too long and was terminated."),
    ("Backup ist beschädigt (integrity_check={0})", "Backup is corrupt (integrity_check={0})"),
    ("Backup liegt außerhalb des lokalen Backup-Speichers.", "Backup is outside the local backup storage."),
    ("Backup-Snapshot ist ungültig (size={0}, integrity={1}); es wurde kein Backup erstellt.",
     "Backup snapshot is invalid (size={0}, integrity={1}); no backup was created."),
    ("Backuppfad ist ungültig.", "Backup path is invalid."),
    ("Bitte bestätigen Sie zunächst den Einwilligungs- und Datenschutzhinweis.",
     "Please acknowledge the consent and data-protection notice first."),
    ("Bitte ein ASR-Modell auswählen.", "Please select an ASR model."),
    ("Bitte eine Frage eingeben.", "Please enter a question."),
    ("DOCX-Export fehlgeschlagen.", "DOCX export failed."),
    ("Das Audio-Profil muss einen gültigen Namen haben.", "The audio profile must have a valid name."),
    ("Das Live-Fenster muss zwischen 6 und 60 Sekunden liegen.", "The live window must be between 6 and 60 seconds."),
    ("Das Rauschprofil konnte nicht gelesen werden.", "The noise profile could not be read."),
    ("Das Rauschprofil liegt außerhalb der Aufnahme.", "The noise profile is outside the recording."),
    ("Das Rauschprofil muss einen gültigen Zeitbereich enthalten.", "The noise profile must contain a valid time range."),
    ("Das Rauschprofil muss innerhalb der Aufnahme liegen und darf höchstens 30 Sekunden lang sein.",
     "The noise profile must lie within the recording and be at most 30 seconds long."),
    ("Das gewählte Audio-Profil ist nicht vorhanden.", "The selected audio profile is not available."),
    ("Das lokale Modell hat keine Antwort geliefert.", "The local model returned no answer."),
    ("Datei liegt nicht im Papierkorb.", "File is not in the trash."),
    ("Dateien einer aktiven Aufnahme können nicht endgültig gelöscht werden.",
     "Files of an active recording cannot be permanently deleted."),
    ("Dateien einer aktiven Aufnahme können nicht gelöscht werden.", "Files of an active recording cannot be deleted."),
    ("Der Anbieter-Endpunkt muss eine HTTP(S)-URL sein.", "The provider endpoint must be an HTTP(S) URL."),
    ("Der Aufgabentext muss Text sein.", "The task text must be text."),
    ("Der Dateiname ist zu lang.", "The file name is too long."),
    ("Der Gerätename ist zu lang.", "The device name is too long."),
    ("Der Gerätename muss Text sein.", "The device name must be text."),
    ("Der Live-Nachlauf muss kürzer als das Live-Fenster sein.", "The live tail must be shorter than the live window."),
    ("Der Live-Nachlauf muss zwischen 0,5 und 10 Sekunden liegen.", "The live tail must be between 0.5 and 10 seconds."),
    ("Der Meetingtitel darf nicht leer sein.", "The meeting title must not be empty."),
    ("Der Meetingtitel ist zu lang.", "The meeting title is too long."),
    ("Der Meetingtitel muss Text sein.", "The meeting title must be text."),
    ("Der Projektname ist zu lang.", "The project name is too long."),
    ("Der Titel ist zu lang.", "The title is too long."),
    ("Der Titel muss Text sein.", "The title must be text."),
    ("Der Upload ist noch nicht vollständig.", "The upload is not complete yet."),
    ("Der Upload würde die angekündigte Dateigröße überschreiten.", "The upload would exceed the announced file size."),
    ("Die Audio-Vorschau ist nicht mehr verfügbar.", "The audio preview is no longer available."),
    ("Die Audio-Vorschau wurde nicht gefunden.", "The audio preview was not found."),
    ("Die Audioaufnahme wurde nicht gefunden.", "The audio recording was not found."),
    ("Die Audiodatei liegt nicht im lokalen Audio-Speicher.", "The audio file is not in the local audio storage."),
    ("Die Audioverbesserung ist deaktiviert oder läuft bereits.", "Audio enhancement is disabled or already running."),
    ("Die Aufgabe ist zu lang.", "The task is too long."),
    ("Die Benchmark-Aufnahme liegt nicht im lokalen Audio-Speicher.", "The benchmark recording is not in the local audio storage."),
    ("Die Deadline muss Text sein.", "The deadline must be text."),
    ("Die Geräte-ID darf nicht negativ sein.", "The device ID must not be negative."),
    ("Die Geräte-ID muss eine Zahl sein.", "The device ID must be a number."),
    ("Die Projektbeschreibung ist zu lang.", "The project description is too long."),
    ("Die Projektbeschreibung muss Text sein.", "The project description must be text."),
    ("Die Projektdatei wurde nicht gefunden.", "The project file was not found."),
    ("Die Transkription hat keinen Text erkannt. Das bisherige Transkript wurde nicht verändert.",
     "The transcription detected no text. The existing transcript was not changed."),
    ("Die Upload-Einstellungen sind beschädigt.", "The upload settings are corrupt."),
    ("Die Upload-Größe muss eine Zahl sein.", "The upload size must be a number."),
    ("Die Wellenform konnte nicht gelesen werden.", "The waveform could not be read."),
    ("Die gespeicherte Transkriptversion enthält doppelte Segment-IDs.", "The stored transcript version contains duplicate segment IDs."),
    ("Die gespeicherte Transkriptversion enthält ungültige Zeitbereiche.", "The stored transcript version contains invalid time ranges."),
    ("Die gespeicherte Transkriptversion enthält ungültige Zeitdaten.", "The stored transcript version contains invalid timing data."),
    ("Die gespeicherte Transkriptversion ist beschädigt.", "The stored transcript version is corrupt."),
    ("Die gespeicherte Transkriptversion ist ungültig.", "The stored transcript version is invalid."),
    ("Die gespeicherte Transkriptversion verweist auf ein fremdes Segment.", "The stored transcript version refers to a foreign segment."),
    ("Die hochgeladene Datei ist leer.", "The uploaded file is empty."),
    ("Die temporäre Uploaddatei ist unvollständig.", "The temporary upload file is incomplete."),
    ("Die temporäre Uploaddatei stimmt nicht mit dem gespeicherten Fortschritt überein.",
     "The temporary upload file does not match the stored progress."),
    ("Dieser LLM-Endpunkt liegt nicht auf diesem Rechner. Aktiviere 'externe Anbieter erlauben' ausdrücklich in den Einstellungen.",
     "This LLM endpoint is not on this machine. Explicitly enable 'allow external providers' in the settings."),
    ("Dieser Upload ist nicht mehr fortsetzbar.", "This upload can no longer be resumed."),
    ("Dieser Upload ist nicht pausiert.", "This upload is not paused."),
    ("Dieser Upload kann nicht abgeschlossen werden.", "This upload cannot be completed."),
    ("Download '{0}' erfordert eine ausdrückliche Bestätigung.", "Download '{0}' requires explicit confirmation."),
    ("Ein Backup kann während einer aktiven Aufnahme nicht wiederhergestellt werden.",
     "A backup cannot be restored during an active recording."),
    ("Ein Meeting muss zuerst in den Papierkorb verschoben werden.", "A meeting must first be moved to the trash."),
    ("Ein Projekt mit aktiver Aufnahme kann nicht endgültig gelöscht werden.",
     "A project with an active recording cannot be permanently deleted."),
    ("Ein Projekt mit aktiver Aufnahme kann nicht gelöscht werden.",
     "A project with an active recording cannot be deleted."),
    ("Ein Projekt muss zuerst in den Papierkorb verschoben werden.", "A project must first be moved to the trash."),
    ("Ein Rauschprofil ist deaktiviert.", "A noise profile is disabled."),
    ("Eine Aufgabe darf nicht leer sein", "A task must not be empty"),
    ("Eine Aufgabe darf nicht leer sein.", "A task must not be empty."),
    ("Eine Aufgabe im Papierkorb kann nicht archiviert werden.", "A task in the trash cannot be archived."),
    ("Eine Aufgabe im Papierkorb muss zuerst wiederhergestellt werden.", "A task in the trash must first be restored."),
    ("Eine Aufgabe muss zuerst in den Papierkorb verschoben werden.", "A task must first be moved to the trash."),
    ("Eine Datei muss zuerst in den Papierkorb verschoben werden.", "A file must first be moved to the trash."),
    ("Einstellungen müssen ein Objekt sein.", "Settings must be an object."),
    ("Endgültiges Löschen muss ausdrücklich bestätigt werden.", "Permanent deletion must be explicitly confirmed."),
    ("Es läuft bereits ein Meeting. Bitte zuerst beenden.", "A meeting is already running. Please stop it first."),
    ("Externe LLM-Endpunkte sind blockiert. Aktiviere zuerst 'externe Anbieter erlauben'.",
     "External LLM endpoints are blocked. First enable 'allow external providers'."),
    ("Falscher Upload-Versatz: erwartet {0}, erhalten {1}.", "Wrong upload offset: expected {0}, got {1}."),
    ("Format '{0}' ist nicht freigeschaltet (erlaubt: {1})", "Format '{0}' is not enabled (allowed: {1})"),
    ("Für das Rauschprofil müssen Anfang und Ende angegeben werden.", "The noise profile requires a start and an end."),
    ("Für das manuelle Rauschprofil muss ein Bereich ausgewählt werden.", "A range must be selected for the manual noise profile."),
    ("Für dieses Meeting ist keine Audioaufnahme vorhanden.", "No audio recording is available for this meeting."),
    ("Kein Mikrofon für die gemeinsame Aufnahme gefunden.", "No microphone found for the shared recording."),
    ("Kein System-Audio-Loopback-Gerät gefunden -- hier nicht verfügbar.", "No system-audio loopback device found -- not available here."),
    ("Kein Transkript vorhanden -- bitte zuerst transkribieren.", "No transcript available -- please transcribe first."),
    ("Kein Transkript vorhanden – bitte zuerst transkribieren.", "No transcript available – please transcribe first."),
    ("Kein Transkript vorhanden. Bitte das Meeting zuerst transkribieren, bevor es analysiert wird.",
     "No transcript available. Please transcribe the meeting first before it is analyzed."),
    ("Keine Originalaufnahme vorhanden (Meeting nicht finalisiert?).", "No original recording available (meeting not finalized?)."),
    ("Keine lokale Aufnahme für den Benchmark vorhanden.", "No local recording available for the benchmark."),
    ("LLM lieferte ungültiges oder unvollständiges Analyse-JSON (auch nach Korrekturversuch). Fehler: {0}. Es wurde nichts gespeichert (keine Fakten erfunden).",
     "LLM returned invalid or incomplete analysis JSON (even after a repair attempt). Error: {0}. Nothing was saved (no facts invented)."),
    ("LLM-Anfrage abgelehnt HTTP {0}: {1}", "LLM request rejected HTTP {0}: {1}"),
    ("LLM-Anfrage fehlgeschlagen ({0}): {1}", "LLM request failed ({0}): {1}"),
    ("LLM-Server ist (noch) belegt und hat nach {0} Versuchen ({1}s Wartezeit pro Versuch) nicht antworten können. Bitte in wenigen Augenblicken erneut versuchen. [{2}]",
     "The LLM server is (still) busy and could not respond after {0} attempts ({1}s wait per attempt). Please try again in a few moments. [{2}]"),
    ("LLM-Server ist belegt. Letzter Hinweis: {0}", "The LLM server is busy. Last hint: {0}"),
    ("LLM-Server nicht erreichbar unter {0} (läuft er?). Details: {1}", "The LLM server is unreachable at {0} (is it running?). Details: {1}"),
    ("LLM-Server-Fehler HTTP {0}: {1}", "LLM server error HTTP {0}: {1}"),
    ("Live-Fenster und Live-Nachlauf müssen Zahlen sein.", "The live window and live tail must be numbers."),
    ("Meeting liegt nicht im Papierkorb.", "Meeting is not in the trash."),
    ("Meeting nicht gefunden: {0}", "Meeting not found: {0}"),
    ("Meeting und Projekt der Aufgabe gehören nicht zusammen.", "The meeting and the task's project do not match."),
    ("Mock-LLM: Server ist belegt (simuliert).", "Mock LLM: server is busy (simulated)."),
    ("Mock-LLM: simulierter Analyse-Fehler.", "Mock LLM: simulated analysis error."),
    ("Netzwerk ist deaktiviert. Aktivieren Sie es in den Einstellungen, um '{0}' auszuführen.",
     "The network is disabled. Enable it in the settings to run '{0}'."),
    ("Nicht unterstützte PCM-Bittiefe: {0} Bit", "Unsupported PCM bit depth: {0} bit"),
    ("Ollama hat die Modellinstallation nicht bestätigt.", "Ollama did not confirm the model installation."),
    ("Ollama war nicht erreichbar oder der Download ist fehlgeschlagen.", "Ollama was unreachable or the download failed."),
    ("Originalaufnahme ist keine gültige WAV-Datei.", "The original recording is not a valid WAV file."),
    ("PDF-Export dauerte zu lange und wurde beendet.", "PDF export took too long and was terminated."),
    ("PDF-Export fehlgeschlagen: {0}", "PDF export failed: {0}"),
    ("Parakeet benötigt eine 16-Bit-WAV-Datei.", "Parakeet requires a 16-bit WAV file."),
    ("Parakeet ist lokal noch nicht bereit. Starte den ausdrücklichen Download in der Modellverwaltung; bis dahin kann faster-whisper als Ersatz verwendet werden.",
     "Parakeet is not ready locally yet. Start the explicit download in the model manager; until then faster-whisper can be used as a fallback."),
    ("Parakeet ist nicht vollständig installiert.", "Parakeet is not fully installed."),
    ("Parakeet-Download fehlgeschlagen: {0}", "Parakeet download failed: {0}"),
    ("Parakeet-Download unvollständig; fehlende Modelldateien.", "Parakeet download incomplete; missing model files."),
    ("Parakeet-Modell konnte nicht geladen werden: {0}", "The Parakeet model could not be loaded: {0}"),
    ("Projekt liegt nicht im Papierkorb.", "Project is not in the trash."),
    ("Projektdateien müssen einem Projekt zugeordnet werden.", "Project files must be assigned to a project."),
    ("Projektname darf nicht leer sein.", "The project name must not be empty."),
    ("Projektname muss Text sein.", "The project name must be text."),
    ("Projektname oder Beschreibung ist zu lang.", "The project name or description is too long."),
    ("Sampleraten müssen positiv sein.", "Sample rates must be positive."),
    ("System-Audio-Aufnahme ist deaktiviert. Aktivieren Sie 'system_audio_enabled', um das zu nutzen.",
     "System-audio capture is disabled. Enable 'system_audio_enabled' to use it."),
    ("Systemaudio kann nicht als Sprachvorschau bearbeitet werden.", "System audio cannot be processed as a voice preview."),
    ("Systemaudio wird nicht automatisch verbessert.", "System audio is not enhanced automatically."),
    ("Tag darf nicht leer sein.", "A tag must not be empty."),
    ("Unbekannte Audio-Version.", "Unknown audio version."),
    ("Unbekannte Audioquelle.", "Unknown audio source."),
    ("Unbekannte Aufgabenansicht", "Unknown task view"),
    ("Unbekannter Projektstatus.", "Unknown project status."),
    ("Unbekannter Rauschprofil-Modus.", "Unknown noise-profile mode."),
    ("Unbekannter Status '{0}' (gültig: {1})", "Unknown status '{0}' (valid: {1})"),
    ("Unbekanntes Parakeet-Modell.", "Unknown Parakeet model."),
    ("Unerwartete Antwort (kein JSON) vom LLM-Server: {0}", "Unexpected response (not JSON) from the LLM server: {0}"),
    ("Unerwartete Antwortstruktur vom LLM-Server: {0}", "Unexpected response structure from the LLM server: {0}"),
    ("Ungültige Auswertungssprache.", "Invalid analysis language."),
    ("Ungültige Mikrofon-Geräte-ID.", "Invalid microphone device ID."),
    ("Ungültige Sprache.", "Invalid language."),
    ("Ungültige Vorlage für KI-Auswertungen.", "Invalid template for AI analyses."),
    ("Ungültiger Ollama-Modellname.", "Invalid Ollama model name."),
    ("Ungültiger Sprecher-Modus.", "Invalid speaker mode."),
    ("Ungültiger Upload-Versatz.", "Invalid upload offset."),
    ("Ungültiger lokaler Modellname.", "Invalid local model name."),
    ("Ungültiger temporärer Uploadpfad.", "Invalid temporary upload path."),
    ("Ungültiges Datum für den Upload.", "Invalid date for the upload."),
    ("Upload ist zu groß (max. {0} MiB).", "Upload is too large (max. {0} MiB)."),
    ("Upload ist zu groß.", "Upload is too large."),
    ("Upload-Chunk ist zu groß.", "Upload chunk is too large."),
    ("Upload-Chunk muss binär sein.", "The upload chunk must be binary."),
    ("Upload-Inhalt muss binär sein.", "The upload content must be binary."),
    ("Verantwortlich muss Text sein.", "The responsible party must be text."),
    ("Zeitüberschreitung beim LLM-Server {0} nach {1}s: {2}", "Timeout at the LLM server {0} after {1}s: {2}"),
    ("Zum Installieren muss ein lokaler Ollama-Endpunkt konfiguriert sein.", "A local Ollama endpoint must be configured to install."),
    ("backup kind muss 'db' oder 'full' sein", "backup kind must be 'db' or 'full'"),
    ("ffmpeg ist für Audio-/Video-Uploads erforderlich.", "ffmpeg is required for audio/video uploads."),
    ("granularitaet muss 'day' oder 'week' sein", "granularity must be 'day' or 'week'"),
    ("live_period_s muss eine Zahl sein.", "live_period_s must be a number."),
    ("mindestens ein Meeting erforderlich", "at least one meeting is required"),
    ("network_allowed muss boolesch sein.", "network_allowed must be a boolean."),
    ("pipeline_max_workers muss eine Zahl sein.", "pipeline_max_workers must be a number."),
    ("pyannote-Diarization ist opt-in und derzeit nicht verfügbar. Installiere 'pyannote.audio' und lade das Pipeline-Modell ('{0}') manuell in den lokalen HF-Cache, oder setze diarization_backend auf 'numpy' (Standard, offline). Es wurde nichts heruntergeladen.",
     "pyannote diarization is opt-in and not available right now. Install 'pyannote.audio' and manually download the pipeline model ('{0}') into the local HF cache, or set diarization_backend to 'numpy' (default, offline). Nothing was downloaded."),
    ("sample_rate muss positiv sein.", "sample_rate must be positive."),
    ("unbekanntes Format: {0}", "unknown format: {0}"),
    ("unzulässiger SQL-Identifier: {0}", "disallowed SQL identifier: {0}"),
    ("{0} darf keine Zugangsdaten in der URL enthalten.", "{0} must not contain credentials in the URL."),
    ("{0} darf nicht leer sein.", "{0} must not be empty."),
    ("{0} ist keine gültige URL.", "{0} is not a valid URL."),
    ("{0} muss boolesch sein.", "{0} must be a boolean."),
    ("{0} muss ein nichtleerer Modellname sein.", "{0} must be a non-empty model name."),
    ("{0} muss eine HTTP(S)-URL mit Host sein.", "{0} must be an HTTP(S) URL with a host."),
]

_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def _build_static() -> dict[str, str]:
    return {de: en for de, en in _ERROR_CATALOG if "{" not in de}


def _build_patterns() -> list[tuple[re.Pattern, str]]:
    out: list[tuple[re.Pattern, str]] = []
    for de, en in _ERROR_CATALOG:
        if "{" not in de:
            continue
        # Split on placeholders; escape literals, insert non-greedy captures.
        parts = _PLACEHOLDER.split(de)
        regex = ""
        for idx, part in enumerate(parts):
            if idx % 2 == 0:
                regex += re.escape(part)
            else:  # placeholder number
                regex += "(.+?)"
        out.append((re.compile("^" + regex + "$"), en))
    return out


_STATIC = _build_static()
_PATTERNS = _build_patterns()


def localize(text: str, lang: str | None = None) -> str:
    """Return ``text`` in the requested language (English), else unchanged.

    ``lang=None`` reads :data:`current_language`. Only English is produced here;
    any non-English language (including the German default) returns the input
    verbatim, which keeps German the safe default."""
    if lang is None:
        lang = current_language.get()
    if lang != "en":
        return text
    hit = _STATIC.get(text)
    if hit is not None:
        return hit
    for pattern, en in _PATTERNS:
        m = pattern.match(text)
        if m:
            try:
                return en.format(*m.groups())
            except (IndexError, KeyError):
                return text
    return text
