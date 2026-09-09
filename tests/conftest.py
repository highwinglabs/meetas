"""Shared fixtures. Everything runs headless: capture logic is driven by
synthetic/file audio sources, never a real microphone; ASR uses a mock engine."""
from __future__ import annotations

import pytest

from core.audio.stream import SyntheticSource
from core.config import Config, get_config, set_config
from core.llm import LLMEngine, MockLLM
from core.logging_setup import setup_logging
from core.live.engine import LiveASREngine
from core.providers.base import ASREngine, ASRError, ASRSegment
from core.service import MeetingService
from core.store.db import _secure_db_file, get_engine, has_table, make_engine
from core.store.models import Base


class MockASREngine(ASREngine):
    """Deterministic, offline ASR engine for tests (no model, no network)."""

    model_name = "mock"

    def __init__(self, segments: list[ASRSegment] | None = None, fail: bool = False):
        self._segments = segments or [
            ASRSegment(0.0, 1.2, "Hallo, alle zusammen.", "de", 0.90),
            ASRSegment(1.5, 3.0, "Lasst uns über das Q2-Budget sprechen.", "de", 0.85),
        ]
        self.fail = fail
        self.transcribe_calls = 0

    def is_model_ready(self) -> bool:
        return True

    def prepare_model(self, allow_download: bool = False) -> None:
        return None

    def transcribe(self, audio_path, language: str | None = None) -> list[ASRSegment]:
        self.transcribe_calls += 1
        if self.fail:
            raise ASRError("simulated ASR failure")
        return self._segments


class MockLiveASREngine(LiveASREngine):
    """Deterministic live engine: one 2 s segment per 2 s of window (0-based).

    Stable across overlapping windows (text depends only on the window index),
    which is what the pipeline's append-only dedup relies on."""

    name = "mock-live"

    def __init__(self, chunk_s: float = 2.0):
        self._chunk = chunk_s

    def is_ready(self) -> bool:
        return True

    def transcribe_window(self, audio, sample_rate: int,
                          language: str | None = None) -> list[ASRSegment]:
        if audio is None or len(audio) == 0:
            return []
        dur = len(audio) / sample_rate
        n = int(dur // self._chunk)
        return [
            ASRSegment(start_s=round(i * self._chunk, 3),
                       end_s=round((i + 1) * self._chunk, 3),
                       text=f"seg{i}", language="de", confidence=0.9)
            for i in range(n)
        ]


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    """Give every test a fresh, isolated storage dir + global config.

    Autouse so every test runs isolated; also exposed by name for tests that
    need the Config object directly."""
    config = Config(base_dir=tmp_path)
    monkeypatch.setenv("MA_BASE_DIR", str(tmp_path))
    # P3: keep the post-stop pipeline OFF by default so the existing test suite
    # stays deterministic (no background worker threads). P3-specific tests
    # enable it explicitly.
    config.auto_pipeline = False
    # Pin the ASR engine so tests are deterministic regardless of the host:
    # the "auto" default probes the GPU stack (Vulkan/CUDA + native bindings),
    # which varies per machine. Auto-resolution is covered by its own tests.
    config.asr_engine = "faster-whisper"
    set_config(config)
    config.ensure_dirs()
    setup_logging("WARNING", config.log_path, to_stderr=False)
    yield config


def _init_db(config: Config) -> None:
    make_engine(config)
    if not has_table("meeting"):
        Base.metadata.create_all(get_engine())
        _secure_db_file(config)


@pytest.fixture
def make_service():
    """Build a MeetingService whose audio source is synthetic (deterministic).

    Pass asr_engine to inject a mock engine (Phase 2 tests); by default the
    service uses the real provider manager (model not downloaded)."""

    def _make(duration_s: float = 1.5, consent: bool = True,
              asr_engine: ASREngine | None = None,
              llm_engine: LLMEngine | None = None,
              live_engine=None, diar_engine=None) -> MeetingService:
        config = get_config()
        _init_db(config)

        def factory(device_id, sr, ch) -> SyntheticSource:
            return SyntheticSource(duration_s, sr, ch, block_seconds=0.05)

        # A mock LLM by default: no test ever performs a real LLM request.
        llm_engine = llm_engine if llm_engine is not None else MockLLM()
        service = MeetingService(config, source_factory=factory,
                                 asr_engine=asr_engine, llm_engine=llm_engine,
                                 live_engine=live_engine, diar_engine=diar_engine)
        if consent:
            service.record_consent(True)
        return service

    return _make


@pytest.fixture
def finalize_meeting(make_service):
    """Run a short synthetic capture to completion; return (service, meeting_id).

    The returned service uses the mock ASR engine by default so transcribe()
    works offline."""

    def _make(duration_s: float = 1.5, title: str = "T",
              asr_engine: ASREngine | None = None,
              llm_engine: LLMEngine | None = None) -> tuple:
        engine = asr_engine or MockASREngine()
        svc = make_service(duration_s=duration_s, asr_engine=engine,
                           llm_engine=llm_engine)
        mid = svc.start_meeting(title=title, source="mic")
        # The synthetic source is fast/finite: let the worker capture it fully
        # before stopping, so at least one chunk exists (deterministic).
        svc._sessions[mid].wait_done(timeout=10)
        svc.stop(mid)
        return svc, mid

    return _make
