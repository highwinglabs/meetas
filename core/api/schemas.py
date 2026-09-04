"""Pydantic request schemas (responses are returned as plain dicts)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class StartMeetingRequest(BaseModel):
    title: str = "Neues Meeting"
    device_id: Optional[int] = None
    source: str = Field(default="mic", pattern="^(mic|system|both)$")
    consent_ack: bool = False
    project_id: Optional[str] = None
    settings: dict = Field(default_factory=dict)


class MeetingUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=512)
    project_id: Optional[str] = None


class ConsentRequest(BaseModel):
    acknowledged: bool = True


class TranscribeRequest(BaseModel):
    language: Optional[str] = None
    model: Optional[str] = None
    allow_download: bool = False


class DownloadModelRequest(BaseModel):
    # A missing flag must never be interpreted as consent for a network
    # download. The UI sends ``confirm=true`` only after the explicit click.
    confirm: bool = False
    model: Optional[str] = None


class BenchmarkRequest(BaseModel):
    model: str = Field(min_length=1, max_length=160)
    meeting_id: Optional[str] = None


class ExportRequest(BaseModel):
    format: str = Field(default="markdown",
                        pattern="^(markdown|txt|json|html|pdf|docx)$")
    include_analysis: bool = True
    include_transcript: bool = True
    section_keys: Optional[list] = None


class AnalyzeRequest(BaseModel):
    kind: str = Field(default="summary", pattern="^(summary|action_items)$")
    system: Optional[str] = None
    model: Optional[str] = None
    template: Optional[str] = Field(default=None, max_length=64)


class TaskUpdateRequest(BaseModel):
    status: Optional[str] = Field(default=None,
                                  pattern="^(offen|laeuft|erledigt)$")
    owner: Optional[str] = Field(default=None, max_length=256)
    text: Optional[str] = Field(default=None, max_length=20000)
    deadline: Optional[str] = Field(default=None, max_length=128)


class TaskCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    meeting_id: Optional[str] = None
    project_id: Optional[str] = None
    responsible: Optional[str] = Field(default=None, max_length=256)
    deadline: Optional[str] = Field(default=None, max_length=128)


class TaskPermanentDeleteRequest(BaseModel):
    """Explicit acknowledgement required before irreversible task deletion."""
    confirm: bool = False


class DevicePreferenceRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=256)
    index: Optional[int] = Field(default=None, ge=0)


class MarkerRequest(BaseModel):
    at_s: float = Field(default=0.0, ge=0)
    text: str = Field(default="", min_length=1, max_length=500)


class TagRequest(BaseModel):
    value: str = Field(min_length=1, max_length=80)


class SpeakerRenameRequest(BaseModel):
    current: str = Field(min_length=1, max_length=120)
    new_name: str = Field(min_length=1, max_length=120)


class SegmentEditRequest(BaseModel):
    text: str = Field(min_length=0, max_length=20000)
    speaker_id: Optional[str] = None


class ExportSelectionRequest(BaseModel):
    format: str = Field(default="markdown",
                        pattern="^(markdown|txt|json|html|pdf|docx)$")
    include_analysis: bool = True
    include_transcript: bool = True
    section_keys: Optional[list] = None  # None = all sections


class MultiSummaryRequest(BaseModel):
    meeting_ids: Optional[list] = None
    since: Optional[str] = None
    until: Optional[str] = None


class CompareRequest(BaseModel):
    meeting_ids: list = Field(min_length=2)


class BackupCreateRequest(BaseModel):
    kind: str = Field(default="db", pattern="^(db|full)$")
    note: str = Field(default="", max_length=512)


class RestoreRequest(BaseModel):
    backup_id: str = Field(min_length=1, max_length=64)
    confirm: bool = False


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=10000)


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=512)
    description: Optional[str] = Field(default=None, max_length=10000)
    status: Optional[str] = Field(default=None, pattern="^(active|archived)$")


class UploadStartRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    total_size: int = Field(gt=0)
    title: Optional[str] = Field(default=None, max_length=512)
    project_id: Optional[str] = None
    start_at: Optional[str] = None
    settings: dict = Field(default_factory=dict)
    content_type: Optional[str] = Field(default=None, max_length=128)


class LlmTestRequest(BaseModel):
    model: Optional[str] = Field(default=None, max_length=256)


class SettingsUpdateRequest(BaseModel):
    values: dict = Field(default_factory=dict)
    # Enabling external (non-loopback) providers is a security-posture change;
    # it requires an explicit confirmation flag, not just a value flip.
    confirm_network_allowed: bool = False


class AudioPreviewRequest(BaseModel):
    start_s: float = Field(default=0.0, ge=0.0)
    duration_s: float = Field(default=12.0, ge=1.0, le=30.0)
    profile: dict = Field(default_factory=dict)
    noise_profile_mode: Literal["disabled", "automatic", "manual"] = "disabled"
    noise_profile_start_s: Optional[float] = Field(default=None, ge=0.0)
    noise_profile_end_s: Optional[float] = Field(default=None, ge=0.0)


class AudioEnhanceRequest(BaseModel):
    profile: dict | None = None
    noise_profile_mode: Literal["disabled", "automatic", "manual"] = "disabled"
    noise_profile_start_s: Optional[float] = Field(default=None, ge=0.0)
    noise_profile_end_s: Optional[float] = Field(default=None, ge=0.0)


class ChatHistoryMessage(BaseModel):
    """A bounded prior turn used only to resolve follow-up questions."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    meeting_id: Optional[str] = None
    project_id: Optional[str] = None
    meeting_ids: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    limit: int = Field(default=25, ge=1, le=100)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=12)


class GeneralChatRequest(BaseModel):
    """A direct question to the selected local LLM, without meeting context."""

    question: str = Field(min_length=1, max_length=20000)
    model: Optional[str] = Field(default=None, max_length=256)
