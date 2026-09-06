"""REST routes (bound to 127.0.0.1 only). Phase 1: health, devices, consent,
meetings (start/pause/resume/stop/status/list/detail)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.api.schemas import (
    AnalyzeRequest, BackupCreateRequest, CompareRequest, ConsentRequest,
    DownloadModelRequest, BenchmarkRequest, ExportRequest, MarkerRequest, MultiSummaryRequest,
    RestoreRequest, SegmentEditRequest, SpeakerRenameRequest,
    StartMeetingRequest, MeetingUpdateRequest, TagRequest, TaskUpdateRequest, TaskCreateRequest, TaskPermanentDeleteRequest, DevicePreferenceRequest, TranscribeRequest,
    ProjectCreateRequest, ProjectUpdateRequest, SettingsUpdateRequest, ChatRequest,
    GeneralChatRequest,
    UploadStartRequest, LlmTestRequest,
    AudioPreviewRequest, AudioEnhanceRequest,
)
from core.config import get_config
from core.llm import LLMError, LLMUnavailableError, ServerBusyError
from core.providers import ASRError, ModelNotReadyError
from core.security.secrets import NetworkBlockedError
from core.service import (
    ActiveMeetingError, ConsentRequiredError, MeetingService, SpeakerMergeConflictError,
    UnknownMeetingError, UnknownTaskError,
)
from core import __version__, i18n

router = APIRouter()

_service: MeetingService | None = None


def set_service(service: MeetingService) -> None:
    global _service
    _service = service


def get_service() -> MeetingService:
    if _service is None:
        raise HTTPException(503, i18n.localize("Service nicht initialisiert"))
    return _service


def _handle(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ConsentRequiredError as exc:
        raise HTTPException(409, i18n.localize(str(exc)))
    except ActiveMeetingError as exc:
        raise HTTPException(409, i18n.localize(str(exc)))
    except SpeakerMergeConflictError as exc:
        raise HTTPException(409, i18n.localize(str(exc)))
    except UnknownMeetingError as exc:
        raise HTTPException(404, i18n.localize(f"Meeting nicht gefunden: {exc.args[0]}"))
    except UnknownTaskError as exc:
        raise HTTPException(404, i18n.localize(f"Aufgabe nicht gefunden: {exc.args[0]}"))
    except ModelNotReadyError as exc:
        raise HTTPException(409, i18n.localize(str(exc)))
    except NetworkBlockedError as exc:
        raise HTTPException(409, i18n.localize(str(exc)))
    # LLM errors (Phase 3): busy/unavailable -> 503 (server-side, retryable),
    # other LLM failures -> 400. These are subclasses of LLMError, so catch
    # them before the base type.
    except ServerBusyError as exc:
        raise HTTPException(503, i18n.localize(str(exc)))
    except LLMUnavailableError as exc:
        raise HTTPException(503, i18n.localize(str(exc)))
    except LLMError as exc:
        raise HTTPException(400, i18n.localize(str(exc)))
    except ASRError as exc:
        raise HTTPException(400, i18n.localize(str(exc)))
    except KeyError as exc:
        # Generic fallback for non-meeting resources (projects, files,
        # uploads, versions, backups): meeting/task have dedicated classes.
        raise HTTPException(404, i18n.localize(f"Objekt mit der ID {exc.args[0]} nicht gefunden."))
    except ValueError as exc:
        raise HTTPException(400, i18n.localize(str(exc)))


@router.get("/health")
def health():
    service = get_service()
    config = get_config()
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": config.ffmpeg_available(),
        "network_allowed": config.network_allowed,
        "sample_rate": config.sample_rate,
    }


@router.get("/integrity")
def integrity_report():
    """Read-only database, file and processing consistency report."""
    return _handle(get_service().integrity_report)


@router.get("/devices")
def devices():
    return {"devices": _handle(get_service().devices)}


@router.get("/devices/preference")
def device_preference():
    return _handle(get_service().device_preference)


@router.put("/devices/preference")
def update_device_preference(body: DevicePreferenceRequest):
    return _handle(get_service().update_device_preference, body.name, body.index)


@router.get("/consent")
def consent():
    service = get_service()
    return {
        "text": service.consent_text(),
        "acknowledged": service.consent_acknowledged(),
    }


@router.post("/consent/ack")
def consent_ack(body: ConsentRequest):
    _handle(get_service().record_consent, body.acknowledged)
    return {"ok": True}


@router.post("/meetings", status_code=201)
def create_meeting(body: StartMeetingRequest):
    service = get_service()
    meeting_id = _handle(
        service.start_meeting,
        title=body.title,
        device_id=body.device_id,
        source=body.source,
        consent_ack=body.consent_ack,
        project_id=body.project_id,
        settings=body.settings,
    )
    return {"id": meeting_id}


@router.get("/meetings")
def list_meetings(project_id: str | None = None):
    return {"meetings": _handle(get_service().list_meetings, project_id)}


@router.delete("/meetings/{meeting_id}")
def trash_meeting(meeting_id: str):
    return _handle(get_service().trash_meeting, meeting_id)


@router.post("/meetings/{meeting_id}/restore")
def restore_meeting(meeting_id: str):
    return _handle(get_service().restore_meeting, meeting_id)


@router.delete("/meetings/{meeting_id}/permanent")
def permanently_delete_meeting(meeting_id: str):
    return _handle(get_service().permanently_delete_meeting, meeting_id)


@router.get("/trash")
def list_trash():
    return _handle(get_service().list_trash)


@router.get("/config")
def config_values():
    return _handle(get_service().settings)


@router.put("/config")
def update_config(body: SettingsUpdateRequest):
    return _handle(get_service().update_settings, body.values,
                   confirm_network_allowed=body.confirm_network_allowed)


@router.get("/models/catalog")
def model_catalog():
    from core.models.catalog import list_catalog
    return {"models": list_catalog(get_service().config)}


@router.get("/projects")
def list_projects(include_archived: bool = False):
    return {"projects": _handle(get_service().list_projects, include_archived)}


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreateRequest):
    return _handle(get_service().create_project, body.name, body.description)


@router.get("/projects/{project_id}")
def project_detail(project_id: str):
    return _handle(get_service().project_detail, project_id)


@router.post("/projects/{project_id}/files/reindex")
def reindex_project_files(project_id: str):
    return _handle(get_service().reindex_project_files, project_id)


@router.delete("/project-files/{file_id}")
def trash_project_file(file_id: str):
    return _handle(get_service().trash_project_file, file_id)


@router.post("/project-files/{file_id}/restore")
def restore_project_file(file_id: str):
    return _handle(get_service().restore_project_file, file_id)


@router.delete("/project-files/{file_id}/permanent")
def permanently_delete_project_file(file_id: str):
    return _handle(get_service().permanently_delete_project_file, file_id)


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdateRequest):
    return _handle(get_service().update_project, project_id, body.name,
                   body.description, body.status)


@router.delete("/projects/{project_id}")
def trash_project(project_id: str):
    return _handle(get_service().trash_project, project_id)


@router.post("/projects/{project_id}/restore")
def restore_project(project_id: str):
    return _handle(get_service().restore_project, project_id)


@router.delete("/projects/{project_id}/permanent")
def permanently_delete_project(project_id: str):
    return _handle(get_service().permanently_delete_project, project_id)


@router.get("/project-files/{file_id}")
def project_file(file_id: str):
    path, name, mime = _handle(get_service().project_file_path, file_id)
    return FileResponse(path, media_type=mime or "application/octet-stream",
                        filename=name)


@router.post("/uploads")
async def upload_file(request: Request, filename: str, title: str | None = None,
                      project_id: str | None = None, start_at: str | None = None):
    """Receive raw file bytes; the UI sends the body, avoiding multipart deps."""
    # Stream with a hard cap so an oversized body can never be read into
    # memory unbounded (DoS / disk exhaustion).
    config = get_service().config
    # The direct endpoint ultimately passes bytes to the import pipeline. Keep
    # that path bounded in RAM; callers with larger files must use resumable
    # uploads, whose chunks are persisted incrementally to disk.
    max_bytes = min(int(config.max_upload_bytes),
                    int(getattr(config, "max_direct_upload_bytes", 64 * 1024 * 1024)))
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > max_bytes:
            raise HTTPException(413, i18n.localize(f"Upload ist zu groß (max. {max_bytes // (1024 * 1024)} MiB direkt)."))
    except ValueError:
        pass
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > max_bytes:
            raise HTTPException(
                413, i18n.localize(f"Upload ist zu groß (max. {max_bytes // (1024 * 1024)} MiB)."))
        content.extend(chunk)
    settings_header = request.headers.get("x-meeting-settings", "{}")
    try:
        import json
        settings = json.loads(settings_header)
    except Exception:
        settings = {}
    return _handle(get_service().import_upload, filename, bytes(content),
                   title=title, project_id=project_id, settings=settings,
                   content_type=request.headers.get("content-type"), start_at=start_at)


@router.post("/uploads/start")
def start_upload(body: UploadStartRequest):
    return _handle(get_service().start_upload, body.filename, body.total_size,
                   title=body.title, project_id=body.project_id,
                   settings=body.settings, content_type=body.content_type,
                   start_at=body.start_at)


@router.get("/uploads")
def list_uploads():
    return {"uploads": _handle(get_service().list_uploads)}


@router.get("/uploads/{upload_id}")
def upload_status(upload_id: str):
    return _handle(get_service().upload_status, upload_id)


@router.patch("/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request):
    raw_offset = request.headers.get("x-upload-offset", "")
    try:
        offset = int(raw_offset)
    except ValueError:
        raise HTTPException(400, i18n.localize("X-Upload-Offset fehlt oder ist ungültig."))
    # Stream the body with a hard per-chunk cap (413 on exceed) instead of
    # `await request.body()`, which would buffer an unbounded request in memory.
    try:
        max_bytes = max(1, int(get_service().config.max_upload_chunk_bytes))
    except (TypeError, ValueError):
        max_bytes = 256 * 1024 * 1024
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > max_bytes:
            raise HTTPException(
                413, i18n.localize(f"Chunk ist zu groß (max. {max_bytes // (1024 * 1024)} MiB pro Chunk)."))
        content.extend(chunk)
    return _handle(get_service().append_upload_chunk, upload_id, offset,
                    bytes(content))


@router.post("/uploads/{upload_id}/pause")
def pause_upload(upload_id: str):
    return _handle(get_service().pause_upload, upload_id)


@router.post("/uploads/{upload_id}/resume")
def resume_upload(upload_id: str):
    return _handle(get_service().resume_upload, upload_id)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str):
    return _handle(get_service().complete_upload, upload_id)


@router.delete("/uploads/{upload_id}")
def cancel_upload(upload_id: str):
    return _handle(get_service().cancel_upload, upload_id)


# --- Phase 7b: collection-level GET routes (must precede /meetings/{meeting_id}) ---
@router.get("/meetings/recurring-topics")
def recurring_topics(min_occurrences: int = 2):
    return _handle(get_service().recurring_topics, min_occurrences)


@router.get("/meetings/timeline")
def timeline(granularity: str = "day", limit: int = 50):
    return _handle(get_service().timeline, granularity, min(limit, 500))


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str):
    return _handle(get_service().get_meeting, meeting_id)


@router.patch("/meetings/{meeting_id}")
def update_meeting(meeting_id: str, body: MeetingUpdateRequest):
    values = body.model_dump(exclude_unset=True)
    return _handle(get_service().update_meeting, meeting_id,
                   title=values.get("title"),
                   project_id=values.get("project_id") if "project_id" in values else None,
                   project_specified="project_id" in values)


@router.get("/meetings/{meeting_id}/transcript-versions/{version_id}")
def transcript_version(meeting_id: str, version_id: str):
    return _handle(get_service().get_transcript_version, meeting_id, version_id)


@router.post("/meetings/{meeting_id}/transcript-versions/{version_id}/restore")
def restore_transcript_version(meeting_id: str, version_id: str):
    return _handle(get_service().restore_transcript_version, meeting_id, version_id)


@router.get("/meetings/{meeting_id}/status")
def meeting_status(meeting_id: str):
    return _handle(get_service().get_status, meeting_id)


@router.post("/meetings/{meeting_id}/pause")
def pause(meeting_id: str):
    _handle(get_service().pause, meeting_id)
    return {"ok": True, "status": "paused"}


@router.post("/meetings/{meeting_id}/resume")
def resume(meeting_id: str):
    _handle(get_service().resume, meeting_id)
    return {"ok": True, "status": "recording"}


@router.post("/meetings/{meeting_id}/stop")
def stop(meeting_id: str):
    return _handle(get_service().stop, meeting_id)


# --- P3: automatic post-stop pipeline ---
@router.get("/meetings/{meeting_id}/pipeline")
def pipeline_status(meeting_id: str):
    return _handle(get_service().pipeline_status, meeting_id)


@router.post("/meetings/{meeting_id}/pipeline")
def run_pipeline(meeting_id: str, wait: bool = False):
    return _handle(get_service().run_pipeline, meeting_id, wait=wait)


# --- Phase 2: ASR / search / export ---
@router.get("/models/asr")
def asr_model_status(model: str | None = None):
    return _handle(get_service().asr_model_status, model)


@router.post("/models/asr/download")
def download_asr_model(body: DownloadModelRequest):
    return _handle(get_service().download_asr_model, body.confirm, body.model)


@router.post("/models/llm/install")
def install_llm_model(body: DownloadModelRequest):
    return _handle(get_service().install_ollama_model, body.model or "", body.confirm)


@router.post("/models/asr/benchmark")
def benchmark_asr(body: BenchmarkRequest):
    return _handle(get_service().benchmark_asr, body.model, body.meeting_id)


@router.post("/models/llm/test")
def test_llm(body: LlmTestRequest):
    return _handle(get_service().test_llm, body.model)


@router.post("/meetings/{meeting_id}/transcribe")
def transcribe(meeting_id: str, body: TranscribeRequest):
    return _handle(
        get_service().transcribe, meeting_id,
        language=body.language, model_name=body.model,
        allow_download=body.allow_download)


@router.post("/meetings/{meeting_id}/transcribe/start", status_code=202)
def start_transcription(meeting_id: str, body: TranscribeRequest):
    return _handle(
        get_service().start_transcription, meeting_id,
        language=body.language, model_name=body.model,
        allow_download=body.allow_download)


@router.get("/search")
def search(q: str, limit: int = 25, project_id: str | None = None,
           meeting_id: str | None = None, meeting_ids: str | None = None):
    ids = [x for x in (meeting_ids or "").split(",") if x]
    results = _handle(get_service().search, q, min(limit, 100), project_id, meeting_id, ids)
    return {"query": q, "count": len(results), "results": results}


@router.get("/meetings/{meeting_id}/export")
def export_meeting(meeting_id: str, format: str = "markdown"):
    return _handle(get_service().export_meeting, meeting_id, format)


@router.get("/meetings/{meeting_id}/audio")
def meeting_audio(meeting_id: str, variant: str = "enhanced"):
    path = _handle(get_service().audio_path, meeting_id, variant)
    return FileResponse(path)


@router.post("/meetings/{meeting_id}/audio/enhance")
def enhance_meeting_audio(meeting_id: str, body: AudioEnhanceRequest | None = None):
    return _handle(get_service().reprocess_audio, meeting_id,
                   body.profile if body is not None else None,
                   body.noise_profile_mode if body is not None else "disabled",
                   body.noise_profile_start_s if body is not None else None,
                   body.noise_profile_end_s if body is not None else None)


@router.get("/meetings/{meeting_id}/audio/waveform")
def meeting_audio_waveform(meeting_id: str, points: int = 240,
                           start_s: float = 0.0, duration_s: float | None = None,
                           variant: str = "original"):
    return _handle(get_service().audio_waveform, meeting_id, points, start_s,
                   duration_s, variant)


@router.post("/meetings/{meeting_id}/audio/preview")
def meeting_audio_preview(meeting_id: str, body: AudioPreviewRequest):
    return _handle(
        get_service().create_audio_preview, meeting_id, body.start_s,
        body.duration_s, body.profile, body.noise_profile_mode,
        body.noise_profile_start_s,
        body.noise_profile_end_s)


@router.get("/meetings/{meeting_id}/audio/preview/{token}")
def meeting_audio_preview_file(meeting_id: str, token: str):
    path = _handle(get_service().audio_preview_path, meeting_id, token)
    return FileResponse(path, media_type="audio/wav")


@router.post("/meetings/{meeting_id}/export")
def export_meeting_selected(meeting_id: str, body: ExportRequest):
    return _handle(
        get_service().export_meeting, meeting_id, body.format,
        include_analysis=body.include_analysis,
        include_transcript=body.include_transcript,
        section_keys=body.section_keys)


# --- Phase 7b: compare, multi-summary, recurring topics, timeline ---
@router.post("/meetings/compare")
def compare_meetings(body: CompareRequest):
    return _handle(get_service().compare_meetings, body.meeting_ids)


@router.post("/meetings/multi-summary")
def multi_summary(body: MultiSummaryRequest):
    return _handle(
        get_service().multi_summary, body.meeting_ids,
        since=body.since, until=body.until)


# NOTE: `GET /meetings/recurring-topics` and `GET /meetings/timeline` are
# registered above `/meetings/{meeting_id}` (below) so they are not shadowed by
# the dynamic id route.

# --- Phase 7c: backup / restore ---
@router.post("/backup")
def create_backup(body: BackupCreateRequest):
    return _handle(get_service().create_backup, body.kind, body.note)


@router.get("/backup")
def list_backups():
    return {"backups": _handle(get_service().list_backups)}


@router.post("/backup/restore")
def restore_backup(body: RestoreRequest):
    return _handle(get_service().restore_backup, body.backup_id, body.confirm)


@router.post("/storage/release")
def release_storage():
    return _handle(get_service().release_storage)


# --- Phase 3: LLM analysis (local server only) ---
@router.get("/models/llm")
def llm_model_status():
    return _handle(get_service().llm_status)


@router.get("/models/providers")
def provider_status():
    """Local provider selection: resolved provider per capability role (no network)."""
    return _handle(get_service().provider_status)


@router.post("/meetings/{meeting_id}/analyze")
def analyze(meeting_id: str, body: AnalyzeRequest):
    return _handle(
        get_service().analyze, meeting_id,
        kind=body.kind, override_system=body.system, model_name=body.model,
        template=body.template)


@router.post("/meetings/{meeting_id}/analysis/cancel")
def cancel_analysis(meeting_id: str):
    return _handle(get_service().cancel_analysis, meeting_id)


# --- Phase 5: live transcription + speaker diarization (local, opt-in) ---
@router.get("/config/features")
def feature_flags():
    return _handle(get_service().feature_flags)


@router.post("/meetings/{meeting_id}/diarize")
def diarize(meeting_id: str):
    return _handle(get_service().diarize, meeting_id)


# --- Local relevance index + grounded RAG ---
@router.post("/meetings/{meeting_id}/embed")
def embed_meeting(meeting_id: str):
    return _handle(get_service().embed_meeting, meeting_id)


@router.get("/ask")
def ask(q: str, limit: int = 25, meeting_id: str | None = None,
        project_id: str | None = None, meeting_ids: str | None = None,
        model: str | None = None):
    ids = [x for x in (meeting_ids or "").split(",") if x]
    out = _handle(get_service().ask, q, min(limit, 100), meeting_id,
                  project_id, ids, model)
    return {"query": q, **out}


@router.post("/chat")
def chat(body: ChatRequest):
    out = _handle(get_service().ask, body.question, body.limit,
                  body.meeting_id, body.project_id, body.meeting_ids, body.model,
                  [{"role": turn.role, "content": turn.content} for turn in body.history])
    return {"query": body.question, **out}


@router.post("/chat/general")
def general_chat(body: GeneralChatRequest):
    """Ask the local LLM directly, deliberately without transcript retrieval."""
    out = _handle(get_service().general_chat, body.question, body.model)
    return {"query": body.question, **out}


# --- Phase 6: central task overview ---
@router.get("/tasks/overview")
def task_overview(view: str = "active"):
    return _handle(get_service().task_overview, view)


@router.get("/tasks/{task_id}/history")
def task_history(task_id: str):
    return {"history": _handle(get_service().task_history, task_id)}


@router.get("/tasks")
def list_tasks(status: str | None = None, owner: str | None = None,
               limit: int = 200, meeting_id: str | None = None,
               project_id: str | None = None, view: str = "active"):
    tasks = _handle(get_service().list_tasks, status, owner, min(limit, 1000), meeting_id, project_id, view)
    return {"count": len(tasks), "tasks": tasks}


@router.post("/tasks", status_code=201)
def create_task(body: TaskCreateRequest):
    return _handle(get_service().create_manual_task, body.text, body.meeting_id,
                   body.project_id, body.responsible, body.deadline)


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdateRequest):
    values = body.model_dump(exclude_unset=True)
    return _handle(get_service().update_task, task_id,
                   status=values.get("status"), owner=values.get("owner"),
                   text=values.get("text"), deadline=values.get("deadline"))


@router.post("/tasks/{task_id}/archive")
def archive_task(task_id: str):
    return _handle(get_service().archive_task, task_id)


@router.post("/tasks/{task_id}/restore")
def restore_task(task_id: str):
    return _handle(get_service().restore_task, task_id)


@router.delete("/tasks/{task_id}")
def trash_task(task_id: str):
    return _handle(get_service().trash_task, task_id)


@router.delete("/tasks/{task_id}/permanent")
def permanently_delete_task(task_id: str, body: TaskPermanentDeleteRequest):
    return _handle(get_service().permanently_delete_task, task_id, body.confirm)


@router.post("/meetings/{meeting_id}/extract-tasks")
def extract_tasks(meeting_id: str):
    return _handle(get_service().extract_tasks_for_meeting, meeting_id)


# --- Phase 4: speaker profiles / rename ---
@router.get("/meetings/{meeting_id}/speakers")
def list_speakers(meeting_id: str):
    return {"meeting_id": meeting_id,
            "speakers": _handle(get_service().list_speakers, meeting_id)}


@router.post("/meetings/{meeting_id}/speakers/rename")
def rename_speaker(meeting_id: str, body: SpeakerRenameRequest):
    return _handle(get_service().rename_speaker, meeting_id, body.current,
                   body.new_name, body.confirm_merge)


# --- Phase 7a: markers, revisions (edits), tags, auto title/tags ---
@router.post("/meetings/{meeting_id}/markers")
def add_marker(meeting_id: str, body: MarkerRequest):
    return _handle(get_service().add_marker, meeting_id, body.at_s, body.text)


@router.get("/meetings/{meeting_id}/markers")
def list_markers(meeting_id: str):
    return {"meeting_id": meeting_id,
            "markers": _handle(get_service().list_markers, meeting_id)}


@router.delete("/markers/{marker_id}")
def delete_marker(marker_id: str):
    return _handle(get_service().delete_marker, marker_id)


@router.post("/meetings/{meeting_id}/segments/{segment_id}/edit")
def edit_segment(meeting_id: str, segment_id: str, body: SegmentEditRequest):
    return _handle(get_service().edit_segment, meeting_id, segment_id,
                   body.text, body.speaker_id)


@router.get("/meetings/{meeting_id}/revisions")
def list_revisions(meeting_id: str):
    return {"meeting_id": meeting_id,
            "revisions": _handle(get_service().list_revisions, meeting_id)}


@router.post("/meetings/{meeting_id}/tags")
def add_tag(meeting_id: str, body: TagRequest):
    tags = _handle(get_service().add_tag, meeting_id, body.value)
    return {"meeting_id": meeting_id, "tags": tags}


@router.delete("/meetings/{meeting_id}/tags/{tag}")
def remove_tag(meeting_id: str, tag: str):
    tags = _handle(get_service().remove_tag, meeting_id, tag)
    return {"meeting_id": meeting_id, "tags": tags}


@router.get("/meetings/{meeting_id}/tags")
def list_tags(meeting_id: str):
    return {"meeting_id": meeting_id,
            "tags": _handle(get_service().list_tags, meeting_id)}


@router.post("/meetings/{meeting_id}/auto-title-tags")
def auto_title_tags(meeting_id: str, force: bool = False):
    return _handle(get_service().apply_auto_title_tags, meeting_id, force)
