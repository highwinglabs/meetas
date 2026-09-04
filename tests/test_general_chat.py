"""Regression tests for direct local chat versus transcript-grounded RAG."""
from __future__ import annotations

from fastapi.testclient import TestClient

from core.api.app import create_app
from core.llm import MockLLM


def test_general_chat_works_without_meetings(make_service):
    llm = MockLLM(text="Berlin ist die Hauptstadt Deutschlands.")
    service = make_service(llm_engine=llm)

    out = service.general_chat("Was ist die Hauptstadt Deutschlands?")

    assert out == {
        "answer": "Berlin ist die Hauptstadt Deutschlands.",
        "model": "mock-llm",
        "local": True,
        "grounded": False,
        "sources": [],
    }
    assert llm.calls == 1
    assert llm.last_prompt == "Was ist die Hauptstadt Deutschlands?"
    assert "keine Meeting-Transkripte" in (llm.last_system or "")


def test_general_chat_endpoint_does_not_call_rag(make_service, monkeypatch):
    llm = MockLLM(text="Eine allgemeine Antwort.")
    service = make_service(llm_engine=llm)

    def fail_if_rag_is_called(*_args, **_kwargs):
        raise AssertionError("RAG must not run for /chat/general")

    monkeypatch.setattr(service, "ask", fail_if_rag_is_called)
    app = create_app(service, ui_dist="/does/not/exist")
    with TestClient(app) as client:
        response = client.post("/chat/general", json={"question": "Allgemeine Frage"})

    assert response.status_code == 200
    assert response.json()["answer"] == "Eine allgemeine Antwort."
    assert response.json()["sources"] == []
    assert response.json()["grounded"] is False


def test_general_chat_busy_is_retryable_503(make_service):
    service = make_service(llm_engine=MockLLM(busy=True))
    app = create_app(service, ui_dist="/does/not/exist")
    with TestClient(app) as client:
        response = client.post("/chat/general", json={"question": "Hallo"})

    assert response.status_code == 503
    assert "belegt" in response.json()["detail"]


def test_general_chat_hides_model_reasoning(make_service):
    service = make_service(llm_engine=MockLLM(
        text="<think>Interne Überlegung</think>\nSichtbare Antwort."))

    out = service.general_chat("Frage")

    assert out["answer"] == "Sichtbare Antwort."


def test_general_chat_rejects_blank_question(make_service):
    service = make_service(llm_engine=MockLLM(text="unused"))
    app = create_app(service, ui_dist="/does/not/exist")
    with TestClient(app) as client:
        response = client.post("/chat/general", json={"question": ""})

    assert response.status_code == 422
