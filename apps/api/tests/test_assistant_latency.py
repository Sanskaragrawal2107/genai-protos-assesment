"""Latency guardrails for the assistant.

These lock in the fixes for the 2-3 minute chat turns:

* reasoning models run with a bounded reasoning budget (and the JSON helper
  caps ``max_completion_tokens`` instead of granting 16k),
* a known-broken meeting provider fails fast instead of re-hanging on every
  turn,
* a scheduling turn does not draft the invitation email twice.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app import llm as llm_module
from app.graphs import assistant_graph
from app.graphs.assistant_graph import execute_tool_call
from app.llm import is_reasoning_model
from app.services import calendar

# --------------------------------------------------------------- model budget


def test_is_reasoning_model_matches_only_reasoning_families():
    assert is_reasoning_model("gpt-5-nano")
    assert is_reasoning_model("GPT-5")
    assert is_reasoning_model("o3-mini")
    assert is_reasoning_model("o4-mini")
    assert not is_reasoning_model("gpt-4o-mini")
    assert not is_reasoning_model("")
    assert not is_reasoning_model(None)


def _fake_settings(**overrides):
    values = {
        "llm_model": "gpt-5-nano",
        "llm_reasoning_effort": "minimal",
        "llm_reasoning_summary": "auto",
        "llm_temperature": 0.1,
        "openai_api_key": "test-key",
        "openai_base_url": "https://api.openai.com/v1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_chat_model_streams_reasoning_summary_via_responses_api(monkeypatch):
    monkeypatch.setattr(assistant_graph, "settings", _fake_settings())

    model = assistant_graph._chat_model()

    assert model.use_responses_api is True
    assert model.reasoning == {"summary": "auto", "effort": "minimal"}
    # Prompts and reasoning summaries must not be stored server-side.
    assert model.store is False


def test_chat_model_can_turn_thinking_off(monkeypatch):
    monkeypatch.setattr(
        assistant_graph,
        "settings",
        _fake_settings(llm_reasoning_summary="off"),
    )

    model = assistant_graph._chat_model()

    # Falls back to Chat Completions (no reasoning display) but keeps the
    # latency fix.
    assert not model.use_responses_api
    assert model.reasoning_effort == "minimal"


def test_chat_model_omits_reasoning_settings_for_classic_models(monkeypatch):
    monkeypatch.setattr(
        assistant_graph,
        "settings",
        _fake_settings(llm_model="gpt-4o-mini", llm_reasoning_effort="low"),
    )

    model = assistant_graph._chat_model()

    assert not model.use_responses_api
    assert model.reasoning_effort is None
    assert model.reasoning is None


def test_split_stream_chunk_separates_thinking_from_answer():
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "weighing "}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "options"}]},
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": " there"},
        ]
    )

    reasoning, answer = assistant_graph.split_stream_chunk(chunk)

    assert reasoning == "weighing options"
    assert answer == "Hello there"


def test_split_stream_chunk_handles_plain_string_and_none():
    from langchain_core.messages import AIMessageChunk

    assert assistant_graph.split_stream_chunk(AIMessageChunk(content="hi")) == ("", "hi")
    assert assistant_graph.split_stream_chunk(None) == ("", "")


def test_split_stream_chunk_ignores_tool_call_blocks():
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content=[],
        additional_kwargs={"tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}]},
    )

    assert assistant_graph.split_stream_chunk(chunk) == ("", "")


async def test_schedule_without_platform_asks_instead_of_guessing(monkeypatch):
    async def _fake_resolve(session, workspace_id, name_or_id):
        return {"id": "cand-1", "full_name": "Ada Lovelace", "email": "ada@example.com"}

    async def _fake_connected(session, workspace_id):
        return {"google_meet", "microsoft_teams"}

    monkeypatch.setattr(assistant_graph, "_resolve_candidate", _fake_resolve)
    monkeypatch.setattr(assistant_graph, "connected_meeting_providers", _fake_connected)

    result = await execute_tool_call(
        name="schedule_interview",
        args={"time_expression": "12th of November at 4 pm"},
        session=None,  # type: ignore[arg-type]
        workspace_id="ws-1",
        recruiter_id=None,
        accumulated_candidates=[],
        accumulated_pending=[],
    )

    # No workspace default exists: the assistant must ask, never guess.
    assert result["status"] == "needs_platform"
    assert "Google Meet" in result["question"]
    assert result["connected_platforms"] == ["google_meet", "microsoft_teams"]


def test_provider_hint_reads_the_recruiters_words():
    hint = assistant_graph.provider_hint_from_message

    assert hint("Teams failed, switch to Google Meet") == "google_meet"
    assert hint("let's use teams please") == "microsoft_teams"
    assert hint("schedule it for tomorrow at 3pm") is None


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}


class _FakeClient:
    def __init__(self):
        self.payload: dict | None = None

    async def post(self, url, *, json, headers, timeout):  # noqa: A002 - mirrors httpx
        self.payload = json
        return _FakeResponse()


async def test_complete_json_bounds_reasoning_budget(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(llm_module, "get_client", lambda: client)
    monkeypatch.setattr(llm_module.llm, "model", "gpt-5-nano")
    monkeypatch.setattr(llm_module.llm, "enabled", True)
    monkeypatch.setattr(llm_module.settings, "llm_reasoning_effort", "low")

    await llm_module.llm.complete_json(system="s", user="u", max_tokens=700)

    assert client.payload is not None
    assert client.payload["reasoning_effort"] == "low"
    # Bounded headroom: the old 16k allowance let a four-line email burn ~1.2k
    # reasoning tokens and ~10s of wall clock.
    assert client.payload["max_completion_tokens"] == 1500
    assert "max_tokens" not in client.payload


async def test_complete_json_keeps_temperature_for_classic_models(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(llm_module, "get_client", lambda: client)
    monkeypatch.setattr(llm_module.llm, "model", "gpt-4o-mini")
    monkeypatch.setattr(llm_module.llm, "enabled", True)

    await llm_module.llm.complete_json(system="s", user="u", max_tokens=700)

    assert client.payload is not None
    assert client.payload["max_tokens"] == 700
    assert "max_completion_tokens" not in client.payload
    assert "reasoning_effort" not in client.payload


# ------------------------------------------------------- provider failure cache


def test_provider_failure_cache_roundtrip():
    calendar.clear_provider_failure("acct-test")
    assert calendar.provider_failure("acct-test") is None

    calendar.remember_provider_failure("acct-test", "Error authenticating with resource")
    cached = calendar.provider_failure("acct-test")
    assert cached is not None and "authenticating" in cached

    calendar.clear_provider_failure("acct-test")
    assert calendar.provider_failure("acct-test") is None


def test_provider_failure_cache_expires():
    calendar._provider_failures["acct-expired"] = (time.monotonic() - 1, "boom")
    assert calendar.provider_failure("acct-expired") is None
    assert "acct-expired" not in calendar._provider_failures


def test_auth_failure_detection():
    from app.composio import IntegrationError

    assert calendar._is_auth_failure(
        IntegrationError("Error authenticating with resource", provider="microsoft_teams")
    )
    assert calendar._is_auth_failure(IntegrationError("401 Unauthorized", provider="google_meet"))
    assert not calendar._is_auth_failure(IntegrationError("rate limit exceeded", provider="gmail"))


# ------------------------------------------------------------- draft dedupe


async def test_scheduling_turn_does_not_draft_invitation_twice(monkeypatch):
    async def _fake_resolve(session, workspace_id, name_or_id):
        return {"id": "cand-1", "full_name": "Ada Lovelace", "email": "ada@example.com"}

    monkeypatch.setattr(assistant_graph, "_resolve_candidate", _fake_resolve)

    existing = {
        "id": "act-1",
        "kind": "send_email",
        "payload": {"candidate_id": "cand-1", "interview_id": "int-1", "subject": "Interview"},
    }

    result = await execute_tool_call(
        name="draft_candidate_email",
        args={"email_type": "interview_invite", "subject": "Interview", "body": "See you then."},
        session=None,  # type: ignore[arg-type] - dedupe happens before any query
        workspace_id="ws-1",
        recruiter_id=None,
        accumulated_candidates=[],
        accumulated_pending=[existing],
    )

    assert result["status"] == "already_drafted"
    assert result["pending_action_id"] == "act-1"
    assert "already drafted" in result["message"]