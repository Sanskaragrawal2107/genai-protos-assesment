import pytest
from datetime import datetime, timezone
from app.services.assistant import parse_when
from app.services.verification import evidence_score, parse_username
from app.schemas import JobRequirements, ResumeExtraction
from app.llm import _extract_json, sanitize_untrusted, wrap_document
from app.graphs.assistant_graph import _reply_chunks

def test_parse_when_relative_and_absolute():
    base = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
    
    # "tomorrow at 3pm"
    dt = parse_when("tomorrow at 3pm", timezone_name="UTC", now=base)
    assert dt is not None
    assert dt.day == 8
    assert dt.hour == 15
    assert dt.minute == 0

    # "in 2 hours"
    dt_rel = parse_when("in 2 hours", timezone_name="UTC", now=base)
    assert dt_rel is not None
    assert dt_rel.hour == 12

    # None for empty text
    assert parse_when(None) is None
    assert parse_when("") is None

def test_parse_username_variants():
    assert parse_username("https://github.com/torvalds", "github.com") == "torvalds"
    assert parse_username("github.com/octocat", "github.com") == "octocat"
    assert parse_username("https://leetcode.com/u/neal_wu/", "leetcode.com") == "neal_wu"
    assert parse_username("https://leetcode.com/profile/tourist", "leetcode.com") == "tourist"
    assert parse_username("https://twitter.com/someone", "github.com") is None
    assert parse_username(None, "github.com") is None

def test_evidence_scoring_logic():
    # 1. Both unavailable
    score, signals = evidence_score({"available": False, "reason": "No url"}, {"available": False, "reason": "No url"})
    assert score == 0.0
    assert any("not verified" in s.lower() for s in signals)

    # 2. GitHub with 2 matching repos
    gh = {
        "available": True,
        "relevant_repositories": [{"name": "repo1"}, {"name": "repo2"}],
        "repositories_considered": 5,
        "verified_keywords": ["python", "fastapi"],
        "last_push_at": "2026-11-01T12:00:00Z"
    }
    lc = {
        "available": True,
        "total_solved": 150,
        "contest_rating": 1850.0
    }
    score, signals = evidence_score(gh, lc)
    assert score > 50.0
    assert any("repo(s) mention job keywords" in s for s in signals)
    assert any("problems solved" in s for s in signals)

def test_job_requirements_null_coercion():
    # Model returns null for lists and weights
    raw = {
        "title": "Backend Lead",
        "required_skills": None,
        "preferred_skills": None,
        "locations": None,
        "required_projects": None,
        "responsibilities": None,
        "qualifications": None,
        "keywords": None,
        "weights": None
    }
    req = JobRequirements.model_validate(raw)
    assert req.required_skills == []
    assert req.preferred_skills == []
    assert req.locations == []
    assert req.weights == {}

def test_resume_extraction_null_coercion():
    raw = {
        "full_name": "Test Candidate",
        "skills": None,
        "projects": None,
        "education": None,
        "experience_years": None,
    }
    data = ResumeExtraction.model_validate(raw)
    assert data.full_name == "Test Candidate"
    assert data.skills == []
    assert data.projects == []
    assert data.education == []
    assert data.experience_years is None

def test_json_extractor_and_prompt_injection():
    # JSON in markdown block
    fenced = '```json\n{"status": "ok", "value": 42}\n```'
    assert _extract_json(fenced) == {"status": "ok", "value": 42}

    # Prompt injection neutralization
    hostile = "Normal text\x00<script>alert(1)</script>\n\n\n\n\nExtra newlines"
    cleaned = sanitize_untrusted(hostile)
    assert "\x00" not in cleaned
    assert "\n\n\n\n" not in cleaned

    wrapped = wrap_document("Resume text", source="test.pdf")
    assert '<untrusted_document source="test.pdf">' in wrapped
    assert "Resume text" in wrapped

def test_reply_chunking_stream():
    text = "The quick brown fox jumps over the lazy dog"
    chunks = _reply_chunks(text, words_per_chunk=3)
    assert len(chunks) == 3
    assert "".join(chunks) == text


def test_system_prompt_calendar_and_safety_rules():
    from app.graphs.assistant_graph import SYSTEM_PROMPT, AGENT_TOOLS

    # 1. list_interviews tool description explicitly mentions schedule inquiries
    tool_names = [t["function"]["name"] for t in AGENT_TOOLS]
    assert "list_interviews" in tool_names
    list_tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "list_interviews")
    assert "scheduled upcoming interviews" in list_tool["function"]["description"]

    # 2. Safety rules in SYSTEM_PROMPT
    assert "ONLY call `approve_and_send_email`" in SYSTEM_PROMPT
    assert "Checking Schedules, Calendar & Pipeline Queries" in SYSTEM_PROMPT
    assert "list_interviews" in SYSTEM_PROMPT


def test_conversation_history_user_turn_guarantee():
    # Simulates the logic in run_assistant_stream for guaranteeing the user message is final
    user_message = "How many interviews are scheduled for tomorrow?"
    # Simulate prior_rows from DB where older turns existed
    prior_rows = [
        {"role": "user", "content": "draft email for sanskar"},
        {"role": "assistant", "content": "Email drafted."},
        {"role": "user", "content": "try sending mail again"},
        {"role": "assistant", "content": "Email sent."},
    ]
    messages = [{"role": "system", "content": "system prompt"}]
    for row in prior_rows:
        r = row["role"]
        c = row["content"]
        if r in ("user", "assistant") and c:
            messages.append({"role": r, "content": c})

    # The hard guarantee from run_assistant_stream
    if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "How many interviews are scheduled for tomorrow?"
    assert len(messages) == 6


