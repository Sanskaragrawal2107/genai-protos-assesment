"""Company profile: saved once, reused automatically in every email.

Covers the pure resolution helpers, the system-prompt block that stops the model
asking for the company name/phone/interviewer, and (when a test database is
configured) the placeholder hydration that injects the saved values.
"""
from __future__ import annotations

from app.graphs.assistant_graph import _profile_context_block
from app.repo import workspace_profile
from app.services.communications import (
    _finalize_filled_body,
    _placeholder_replacements,
    _template,
    hydrate_placeholders,
)

EMPTY = {"company_name": "", "company_phone": "", "interviewer_name": ""}


def test_placeholder_map_fills_phone_from_saved_profile():
    replacements = _placeholder_replacements(
        date_label="Monday, 10 November 2025",
        time_label="3:00 PM",
        tz_label="IST",
        has_start=True,
        prov_label="Microsoft Teams",
        meet_url="https://teams.example/join",
        resolved_company="Acme",
        resolved_interviewer="Priya Sharma",
        company_phone="+1 415 555 0134",
        sender="recruiter@acme.com",
        omit_contact=False,
    )

    # The phone is the whole point: the assistant emits the token, the server
    # fills it from the saved profile. Regression guard for the duplicate-key
    # bug where "[Phone]" mapped to "" and the number never appeared.
    assert replacements["[Phone]"] == "+1 415 555 0134"
    assert replacements["[Phone Number]"] == "+1 415 555 0134"
    assert replacements["[Company Phone]"] == "+1 415 555 0134"
    assert replacements["[Company Number]"] == "+1 415 555 0134"


def test_placeholder_map_phone_blank_when_not_saved():
    replacements = _placeholder_replacements(
        date_label="",
        time_label="",
        tz_label="IST",
        has_start=False,
        prov_label="",
        meet_url="",
        resolved_company="Acme",
        resolved_interviewer="",
        company_phone="",
        sender="",
        omit_contact=False,
    )

    # No saved phone: never fabricate one.
    for key in ("[Phone]", "[Phone Number]", "[Company Phone]", "[Company Number]"):
        assert replacements[key] == ""


def test_finalize_drops_empty_phone_label_without_a_number():
    body = "Hi Ada,\n\nBest regards,\nPriya Sharma\nAcme\nPhone:\n"
    out = _finalize_filled_body(body, company_phone="", sender="", omit_contact=False)

    assert "Phone:" not in out
    assert "Priya Sharma" in out
    assert "Acme" in out


def test_finalize_keeps_phone_label_when_a_number_is_present():
    body = "Best regards,\nPriya Sharma\nAcme\n+1 415 555 0134\n"
    out = _finalize_filled_body(
        body, company_phone="+1 415 555 0134", sender="", omit_contact=False
    )

    assert "+1 415 555 0134" in out


def test_workspace_profile_reads_and_trims_saved_settings():
    ws = {
        "settings": {
            "company_name": "  Acme Technologies ",
            "company_phone": " +1 415 555 0134 ",
            "interviewer_name": " Priya Sharma ",
            "require_approval": True,
        }
    }

    assert workspace_profile(ws) == {
        "company_name": "Acme Technologies",
        "company_phone": "+1 415 555 0134",
        "interviewer_name": "Priya Sharma",
    }


def test_workspace_profile_handles_missing_or_unrelated_settings():
    assert workspace_profile(None) == EMPTY
    assert workspace_profile({"settings": {"require_approval": True}}) == EMPTY
    assert workspace_profile({"settings": None}) == EMPTY


def test_profile_context_block_includes_values_and_stops_the_question():
    block = _profile_context_block(
        {"company_name": "Acme", "company_phone": "+1 555", "interviewer_name": "Priya"}
    )

    assert "Acme" in block
    assert "Priya" in block
    assert "+1 555" in block
    # The whole point: the model must never ask for these again.
    assert "never ask" in block.lower()
    # Phone stays a server-filled placeholder.
    assert "[Phone]" in block


def test_template_signs_with_saved_company_profile():
    _subject, body = _template(
        kind="rejection",
        candidate={"full_name": "Ada Lovelace"},
        recruiter_name="recruiter@acme.com",
        job_title="Backend Engineer",
        interview=None,
        notes=None,
        company_name="Acme",
        interviewer_name="Priya Sharma",
        company_phone="+1 415 555 0134",
    )

    assert "Best regards," in body
    assert "Priya Sharma" in body
    assert "Acme" in body
    assert "+1 415 555 0134" in body
    # No raw tokens survive into the template output.
    assert "[" not in body


def test_template_omits_phone_when_not_saved():
    _subject, body = _template(
        kind="reminder",
        candidate={"full_name": "Ada Lovelace"},
        recruiter_name="Recruiter",
        job_title="Backend Engineer",
        interview=None,
        notes=None,
        company_name="Acme",
        interviewer_name="",
        company_phone="",
    )

    assert "Acme" in body
    assert "Best regards," in body


async def test_hydrate_placeholders_fills_saved_company_profile(db, workspace):
    from app.repo import set_workspace_profile

    await set_workspace_profile(
        db,
        workspace,
        company_name="Acme",
        company_phone="+1 415 555 0134",
        interviewer_name="Priya Sharma",
    )

    _subject, body, info = await hydrate_placeholders(
        db,
        workspace_id=workspace,
        candidate={"id": "no-such-candidate", "full_name": "Ada Lovelace"},
        subject="Your interview",
        body="Best regards,\n[Interviewer Name]\n[Company]\n[Phone]\n",
    )

    assert "Priya Sharma" in body
    assert "Acme" in body
    assert "+1 415 555 0134" in body
    assert "[" not in body
    assert info["interviewer"] == "Priya Sharma"
    assert info["company"] == "Acme"
    assert info["company_phone"] == "+1 415 555 0134"
