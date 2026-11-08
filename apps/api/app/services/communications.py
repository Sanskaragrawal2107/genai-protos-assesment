"""Outbound candidate communication (Gmail) with approval gating.

Policy enforced in code:

* every external send is created as ``pending_approval`` first;
* sending happens only through :func:`send_communication`, which is reached via
  an explicit recruiter approval (or ``approve=True`` on the API call);
* the exact body that was approved is the body that is sent;
* both outcome and failure are persisted, so the UI never implies delivery that
  did not happen.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..composio import IntegrationError
from ..db import candidate_communications, candidates, utcnow
from ..llm import LLMError, LLMUnavailable, llm
from ..repo import find_account, log_activity
from .mail import get_mail_provider

log = logging.getLogger("recruiter.comms")

_PROVIDER_LABELS = {"microsoft_teams": "Microsoft Teams", "google_meet": "Google Meet"}


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def hydrate_placeholders(
    session: AsyncSession,
    *,
    workspace_id: str,
    candidate: dict[str, Any],
    subject: str,
    body: str,
    company_name: str | None = None,
    interviewer_name: str | None = None,
    sender_email: str | None = None,
    omit_contact: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    """Replace ``[PLACEHOLDER]`` tokens with values from real records only.

    Shared by the assistant's draft step and the send-time safety net so a
    candidate never receives a raw bracket token. Every value comes from the
    scheduled interview row, the workspace name, or the recruiter's own account;
    a value that is genuinely unknown becomes an explicit "to be confirmed"
    marker instead of a fabricated date, link or name.
    """
    from ..db import interviews as interviews_table, workspaces as workspaces_table

    int_row = (
        await session.execute(
            sa.select(interviews_table)
            .where(
                interviews_table.c.workspace_id == workspace_id,
                interviews_table.c.candidate_id == candidate["id"],
                interviews_table.c.status.in_(("scheduled", "rescheduled")),
            )
            .order_by(interviews_table.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()

    sender = (sender_email or "").strip()
    sender_name = sender.split("@")[0].replace(".", " ").title() if sender else ""

    resolved_company = (company_name or "").strip()
    if not resolved_company:
        ws_row = (
            await session.execute(
                sa.select(workspaces_table.c.name).where(workspaces_table.c.id == workspace_id)
            )
        ).first()
        resolved_company = (ws_row[0] if ws_row and ws_row[0] else "") or ""

    resolved_interviewer = (interviewer_name or "").strip() or sender_name

    meet_url = (int_row.get("meeting_url") if int_row else "") or ""
    prov_label = _PROVIDER_LABELS.get((int_row.get("provider") if int_row else "") or "", "")
    tz_name = (int_row.get("timezone") if int_row else None) or "Asia/Kolkata"
    tz_label = "IST" if "kolkata" in tz_name.lower() else tz_name
    start_time = _coerce_dt(int_row.get("start_time")) if int_row else None

    date_label = ""
    time_label = ""
    if start_time:
        try:
            st_local = start_time.astimezone(ZoneInfo(tz_name))
            date_label = st_local.strftime("%A, %d %B %Y")
            time_label = st_local.strftime("%I:%M %p").lstrip("0")
        except Exception:  # noqa: BLE001 - unknown tz should not break the send
            pass

    tbc = "to be confirmed"
    replacements = {
        "[DATE]": date_label or tbc,
        "[Date]": date_label or tbc,
        "[date]": date_label or tbc,
        "[TIME]": time_label or tbc,
        "[Time]": time_label or tbc,
        "[time]": time_label or tbc,
        "[TIMEZONE]": (tz_label if start_time else "") or tbc,
        "[Timezone]": (tz_label if start_time else "") or tbc,
        "[Time Zone]": (tz_label if start_time else "") or tbc,
        "[timezone]": (tz_label if start_time else "") or tbc,
        "[MEETING_PROVIDER]": prov_label or tbc,
        "[Meeting Provider]": prov_label or tbc,
        "[Platform]": prov_label or tbc,
        "[JOIN_LINK]": meet_url or tbc,
        "[Join Link]": meet_url or tbc,
        "[Join link]": meet_url or tbc,
        "[join_link]": meet_url or tbc,
        "[MEETING_LINK]": meet_url or tbc,
        "[Meeting Link]": meet_url or tbc,
        "[LINK]": meet_url or tbc,
        "[Link]": meet_url or tbc,
        "[Interviewer Name]": resolved_interviewer or tbc,
        "[Interviewer]": resolved_interviewer or tbc,
        "[Reviewer]": resolved_interviewer or tbc,
        "[Reviewer Name]": resolved_interviewer or tbc,
        "[Your Name]": resolved_interviewer or tbc,
        "[Recruiter Name]": resolved_interviewer or tbc,
        "[Company]": resolved_company or tbc,
        "[Company Name]": resolved_company or tbc,
        "[Title]": "Recruiting Team",
        "[Your Title]": "Recruiting Team",
        "[Phone]": "",
        "[Email]": "" if omit_contact else sender,
        "[LinkedIn]": "",
    }

    def _fill(text: str) -> str:
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    h_subj = _fill(subject)
    h_body = _fill(body)

    import re

    h_body = re.sub(r"\n\s*\[(Phone|LinkedIn|Title|Company|Your Name|Email)\]", "", h_body)
    if omit_contact and sender:
        h_body = h_body.replace(sender, "")
    h_body = re.sub(r"\n{3,}", "\n\n", h_body).strip()

    if meet_url and meet_url not in h_body:
        h_body += (
            f"\n\nMeeting Details:\n"
            f"- Platform: {prov_label or 'Video call'}\n"
            f"- Date & Time: {date_label} at {time_label} {tz_label}\n"
            f"- Join link: {meet_url}\n"
            f"(Note: both the interviewer/reviewer and the candidate join through this same link)"
        )

    info = {
        "date": date_label,
        "time": time_label,
        "timezone": tz_label,
        "provider": prov_label,
        "join_url": meet_url,
        "company": resolved_company,
        "interviewer": resolved_interviewer,
        "interviewer_email": sender if not omit_contact else None,
    }
    return h_subj, h_body, info

EMAIL_KINDS = {
    "follow_up": "Follow-up",
    "interview_confirmation": "Interview confirmation",
    "reschedule": "Reschedule notice",
    "rejection": "Rejection",
    "reminder": "Reminder",
    "custom": "Message",
}

_DRAFT_SYSTEM = (
    "You write concise, professional recruiting emails. Return JSON with keys "
    "'subject' and 'body'. The body is plain text, under 180 words, friendly and "
    "specific, and must only contain facts supplied in the input. Never invent an "
    "interview time, a meeting link, a salary or a company detail. Sign off with the "
    "recruiter's name if provided."
)


def _template(
    *,
    kind: str,
    candidate: dict[str, Any],
    recruiter_name: str,
    job_title: str | None,
    interview: dict[str, Any] | None,
    notes: str | None,
) -> tuple[str, str]:
    first_name = (candidate.get("full_name") or "there").split(" ")[0]
    role = job_title or candidate.get("applied_role") or candidate.get("current_role") or "this role"

    if kind == "interview_confirmation" and interview:
        when = interview.get("start_label") or interview.get("start_time")
        link = interview.get("meeting_url")
        provider_label = (
            "Microsoft Teams"
            if interview.get("provider") == "microsoft_teams"
            else ("Google Meet" if interview.get("provider") == "google_meet" else "Video Meeting")
        )
        body = [
            f"Hi {first_name},",
            "",
            f"Great news — we would like to invite you to an interview for {role}.",
            "",
            f"Date & Time: {when}",
            f"Platform: {provider_label}",
        ]
        if link:
            body.append(f"Meeting Join Link: {link}")
            body.append("(Both interviewer/reviewer and candidate join directly using this link)")
        else:
            body.append(
                "The video meeting link will follow in a separate email once the recruiter "
                "confirms the room."
            )
        if notes:
            body += ["", notes]
        body += [
            "",
            "Please reply if you need a different slot and we will rearrange.",
            "",
            "Best regards,",
            recruiter_name,
        ]
        return f"Interview scheduled — {role}", "\n".join(body)

    if kind == "reschedule" and interview:
        when = interview.get("start_label") or interview.get("start_time")
        link = interview.get("meeting_url")
        body = [
            f"Hi {first_name},",
            "",
            f"I need to move our interview for {role}.",
            "",
            f"New time: {when}",
        ]
        if link:
            body.append(f"Join link: {link}")
        body += [
            "",
            "Apologies for the change — please let me know if the new time does not work.",
            "",
            "Best regards,",
            recruiter_name,
        ]
        return f"Updated interview time — {role}", "\n".join(body)

    if kind == "rejection":
        body = [
            f"Hi {first_name},",
            "",
            f"Thank you for taking the time to apply for {role} and for sharing your background "
            "with us.",
            "",
            "After reviewing your experience against the current requirements, we will not be "
            "moving forward with your application at this stage. This is not a reflection of your "
            "abilities, and we would be glad to stay in touch for future openings.",
            "",
            "Best regards,",
            recruiter_name,
        ]
        return f"Update on your application — {role}", "\n".join(body)

    if kind == "reminder":
        body = [
            f"Hi {first_name},",
            "",
            f"Just a quick reminder about our conversation regarding {role}. "
            "If you are still interested, reply with a good time this week and I will set it up.",
            "",
            "Best regards,",
            recruiter_name,
        ]
        return f"Following up — {role}", "\n".join(body)

    body = [
        f"Hi {first_name},",
        "",
        notes
        or f"I wanted to follow up on your application for {role}. Your background looks "
        "relevant and I would like to understand your current availability.",
        "",
        "If you are still exploring opportunities, reply with a couple of times that suit you "
        "and I will send an invite.",
        "",
        "Best regards,",
        recruiter_name,
    ]
    return f"Following up on your application — {role}", "\n".join(body)


async def draft_email(
    *,
    kind: str,
    candidate: dict[str, Any],
    recruiter_name: str = "Recruiting team",
    job_title: str | None = None,
    interview: dict[str, Any] | None = None,
    notes: str | None = None,
    instructions: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(subject, body, method)``. LLM when available, template otherwise."""
    subject, body = _template(
        kind=kind,
        candidate=candidate,
        recruiter_name=recruiter_name,
        job_title=job_title,
        interview=interview,
        notes=notes,
    )
    if not llm.enabled:
        return subject, body, "template"

    payload = {
        "kind": kind,
        "candidate": {
            "name": candidate.get("full_name"),
            "role": candidate.get("current_role"),
            "skills": (candidate.get("skills") or [])[:12],
        },
        "job_title": job_title,
        "interview": interview,
        "recruiter_name": recruiter_name,
        "extra_instructions": instructions,
        "baseline_draft": {"subject": subject, "body": body},
    }
    prompt = (
        "Improve this recruiting email. Keep every factual detail identical to the baseline "
        "draft; you may only adjust tone and wording.\n"
        + json.dumps(payload, default=str)[:6000]
    )
    try:
        result = await llm.complete_json(system=_DRAFT_SYSTEM, user=prompt, max_tokens=700)
        llm_subject = str(result.get("subject") or subject).strip()[:200]
        llm_body = str(result.get("body") or body).strip()
        if len(llm_body) > 40 and (interview is None or (interview.get("meeting_url") or "") in llm_body or not interview.get("meeting_url")):
            return llm_subject, llm_body, "llm"
        return subject, body, "template"
    except (LLMUnavailable, LLMError) as exc:
        log.info("email drafting fell back to template: %s", exc.__class__.__name__)
        return subject, body, "template"


async def queue_email(
    session: AsyncSession,
    *,
    workspace_id: str,
    candidate: dict[str, Any],
    recruiter_id: str | None,
    kind: str,
    subject: str,
    body: str,
    metadata: dict[str, Any] | None = None,
    method: str = "template",
) -> dict[str, Any]:
    comm_id = __import__("uuid").uuid4().hex
    now = utcnow()
    await session.execute(
        sa.insert(candidate_communications).values(
            id=comm_id,
            workspace_id=workspace_id,
            candidate_id=candidate["id"],
            recruiter_id=recruiter_id,
            channel="email",
            direction="outbound",
            kind=kind,
            subject=subject,
            content=body,
            provider="gmail",
            provider_message_id=None,
            status="pending_approval",
            metadata={**(metadata or {}), "draft_method": method},
            created_at=now,
        )
    )
    await session.commit()
    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="email.drafted",
        entity_type="candidate",
        entity_id=candidate["id"],
        metadata={"kind": kind, "subject": subject, "method": method},
    )
    await session.commit()
    return {"id": comm_id, "subject": subject, "content": body, "status": "pending_approval"}


async def send_communication(
    session: AsyncSession,
    *,
    workspace_id: str,
    communication_id: str,
    recruiter_id: str | None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(candidate_communications).where(
                candidate_communications.c.id == communication_id,
                candidate_communications.c.workspace_id == workspace_id,
            )
        )
    ).mappings().first()
    if not row:
        raise ValueError("Communication not found")
    if row["status"] == "sent":
        return {"id": communication_id, "status": "sent", "already_sent": True}

    candidate = (
        await session.execute(
            sa.select(candidates).where(
                candidates.c.id == row["candidate_id"], candidates.c.workspace_id == workspace_id
            )
        )
    ).mappings().first()
    if not candidate or not candidate["email"]:
        raise ValueError("Candidate has no email address on file")

    account = await find_account(session, workspace_id, "gmail")
    provider = get_mail_provider(account)

    await session.execute(
        sa.update(candidate_communications)
        .where(candidate_communications.c.id == communication_id)
        .values(status="sending")
    )
    await session.commit()

    body_text = row["content"] or ""
    subject_text = row["subject"] or ""

    if "[" in body_text and "]" in body_text:
        # Last-resort safety net: never let a raw [PLACEHOLDER] reach Gmail.
        subject_text, body_text, _ = await hydrate_placeholders(
            session,
            workspace_id=workspace_id,
            candidate=dict(candidate),
            subject=subject_text,
            body=body_text,
            sender_email=account.get("email") if account else None,
        )
        # Keep the persisted row identical to what is actually sent.
        await session.execute(
            sa.update(candidate_communications)
            .where(candidate_communications.c.id == communication_id)
            .values(content=body_text, subject=subject_text)
        )
        await session.commit()

    try:
        result = await provider.send_email(
            to=candidate["email"], subject=subject_text, body=body_text
        )
    except IntegrationError as exc:
        await session.execute(
            sa.update(candidate_communications)
            .where(candidate_communications.c.id == communication_id)
            .values(status="failed", error_message=str(exc)[:500])
        )
        await session.commit()
        await log_activity(
            session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            action="email.failed",
            entity_type="candidate",
            entity_id=row["candidate_id"],
            metadata={"error": str(exc)[:300]},
        )
        await session.commit()
        raise

    now = utcnow()
    await session.execute(
        sa.update(candidate_communications)
        .where(candidate_communications.c.id == communication_id)
        .values(
            status="sent",
            sent_at=now,
            provider_message_id=result.get("provider_message_id"),
            metadata={
                **(row["metadata"] or {}),
                "provider": provider.name,
            },
        )
    )
    await session.commit()
    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="email.sent",
        entity_type="candidate",
        entity_id=row["candidate_id"],
        metadata={
            "kind": row["kind"],
            "subject": row["subject"],
        },
    )
    await session.commit()
    return {
        "id": communication_id,
        "status": "sent",
        "provider_message_id": result.get("provider_message_id"),
    }


async def recent_communications(session: AsyncSession, workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    stmt = (
        sa.select(candidate_communications, candidates.c.full_name)
        .join(candidates, candidates.c.id == candidate_communications.c.candidate_id)
        .where(candidate_communications.c.workspace_id == workspace_id)
        .order_by(candidate_communications.c.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).mappings().all()
    out = []
    for row in rows:
        item = dict(row)
        item["candidate_name"] = row.get("full_name")
        out.append(item)
    return out


def format_when(value: datetime | None, timezone_name: str) -> str:
    if not value:
        return "time to be confirmed"
    try:
        from zoneinfo import ZoneInfo

        local = value.astimezone(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001
        local = value
    return local.strftime("%A %d %B %Y, %I:%M %p %Z")
