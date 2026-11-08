"""Conversational AI Recruiter Assistant.

Orchestration is LangChain's ``create_agent``; external app tools and per-user
auth come from a Composio session (``Composio.create(...).tools()``). Recruiter
actions stay as thin LangChain tools so candidate data and approvals persist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timezone
from zoneinfo import ZoneInfo
from typing import Any, AsyncIterator

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as db_module
from ..composio import IntegrationError
from ..config import settings
from ..db import (
    candidate_communications,
    candidates,
    chat_messages,
    chat_threads,
    pending_actions,
    utcnow,
)
from ..repo import (
    candidate_summary,
    create_pending_action,
    list_candidates,
    meeting_provider_for,
    normalize_meeting_provider,
    set_candidate_status,
    to_jsonable,
)
from ..services import assistant as svc
from ..services import communications as comms
from ..services import interviews as interview_svc
from ..services import verification
from ..services.interviews import upcoming_interviews

# Composio owns tool discovery + per-user auth (Connect Links); LangChain owns
# the tool-calling loop. This replaces the hand-written OpenAI tool-call parser
# and the bespoke calendar provider classes.
from composio import Composio
from composio_langchain import LangchainProvider
from json_schema_to_pydantic import create_model as _schema_model
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

log = logging.getLogger("recruiter.assistant_agent")

_TOKEN_RE = re.compile(r"\S+\s*")


def _reply_chunks(text: str, *, words_per_chunk: int = 3) -> list[str]:
    words = _TOKEN_RE.findall(text)
    if not words:
        return [text] if text else []
    return ["".join(words[i : i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]


# --------------------------------------------------------------------- OpenAI Tools
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_candidates",
            "description": "Search candidates from the workspace database matching roles, skills, experience, or keywords.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Job title or search keywords, e.g. 'AI Engineer', 'Python developer', 'LangGraph'",
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required skills list, e.g. ['python', 'langgraph', 'fastapi']",
                    },
                    "location": {"type": "string", "description": "Candidate location filter"},
                    "min_experience_years": {"type": "number", "description": "Minimum years of experience"},
                    "status": {"type": "string", "description": "Pipeline status (APPLIED, SCREENED, SHORTLISTED, etc.)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candidate_profile",
            "description": "Retrieve the full profile, resume summary, contact info, and notes for a specific candidate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {"type": "string", "description": "Candidate full name or UUID"},
                },
                "required": ["candidate_name_or_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_candidate_email",
            "description": (
                "Draft an email to a candidate (shortlist notification, interview invitation, follow-up, "
                "rejection, or custom update). Saves the draft in candidate_communications and queues a pending action "
                "for the recruiter to approve before sending via Gmail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {
                        "type": "string",
                        "description": "Candidate full name or UUID. Deduce from conversation context if not explicitly mentioned.",
                    },
                    "email_type": {
                        "type": "string",
                        "enum": ["shortlist", "interview_invite", "follow_up", "rejection", "custom"],
                        "description": "Category of the email",
                    },
                    "subject": {"type": "string", "description": "Clear, professional email subject line"},
                    "body": {"type": "string", "description": "The complete email body text"},
                    "company_name": {"type": "string", "description": "Company name to sign the email with, if provided by the recruiter"},
                    "interviewer_name": {"type": "string", "description": "Interviewer or host name for the email, if provided by the recruiter"},
                    "omit_contact_info": {"type": "boolean", "description": "Set true if recruiter requested omitting phone/email in signoff"},
                },
                "required": ["subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_interview",
            "description": (
                "Propose or schedule an interview meeting. Supports Microsoft Teams and Google Meet / Calendar. "
                "Creates the real meeting join link, schedules the calendar invite, drafts the invitation email, "
                "and queues it as a pending action for recruiter approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {
                        "type": "string",
                        "description": "Candidate full name or UUID. Deduce from conversation history if not specified.",
                    },
                    "time_expression": {
                        "type": "string",
                        "description": "Date and time in natural language (e.g. '12th of November at 4 pm', 'tomorrow at 3pm')",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Interview duration in minutes (default 30)",
                    },
                    "meeting_provider": {
                        "type": "string",
                        "enum": ["microsoft_teams", "google_meet"],
                        "description": "Video meeting provider. Defaults to the workspace's configured provider. Pass google_meet whenever the recruiter asks to use or switch to Google Meet.",
                    },
                    "title": {"type": "string", "description": "Title of the interview"},
                },
                "required": ["time_expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_candidate",
            "description": (
                "Verify candidate technical credentials: checks LeetCode (solved counts, ranking) and GitHub repositories. "
                "It also fetches the candidate's personal website/portfolio and returns the page's visible text, headings/projects, "
                "contact links, and any GitHub/LeetCode/LinkedIn profiles found there. If the recruiter supplies any profile URL "
                "(leetcode_url / github_url / profile_url), pass it so it is saved on the candidate and actually checked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {"type": "string", "description": "Candidate name or UUID"},
                    "leetcode_url": {
                        "type": "string",
                        "description": "LeetCode profile URL supplied by the recruiter, e.g. https://leetcode.com/u/<handle>/. Saved to the candidate and used for verification.",
                    },
                    "github_url": {
                        "type": "string",
                        "description": "GitHub profile URL supplied by the recruiter, e.g. https://github.com/<handle>. Saved to the candidate and used for verification.",
                    },
                    "profile_url": {
                        "type": "string",
                        "description": "Personal website / portfolio URL supplied by the recruiter. Saved to the candidate; the tool fetches the page, returns its projects/text/links, and discovers any GitHub/LeetCode profiles it links to.",
                    },
                },
                "required": ["candidate_name_or_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_candidate_status",
            "description": "Move a candidate to a new recruitment pipeline stage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {"type": "string", "description": "Candidate full name or UUID"},
                    "status": {
                        "type": "string",
                        "enum": ["APPLIED", "SCREENED", "SHORTLISTED", "INTERVIEWING", "OFFERED", "REJECTED", "ON_HOLD"],
                        "description": "Target pipeline stage",
                    },
                },
                "required": ["candidate_name_or_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_interviews",
            "description": "Check the calendar for upcoming interviews.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_expression": {"type": "string", "description": "Filter window e.g. 'tomorrow' or null for all"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pipeline_summary",
            "description": "Get current recruitment pipeline totals and metrics.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_and_send_email",
            "description": (
                "Approve and immediately send the drafted email or interview confirmation to the candidate "
                "via the connected Gmail account. Call this whenever the recruiter says 'send it', 'send the mail', "
                "'approve and send', 'no send it for now', 'yes send it', 'go ahead and send', or confirms to dispatch the email."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_name_or_id": {
                        "type": "string",
                        "description": "Candidate full name or UUID. Deduce from conversation context if omitted.",
                    },
                    "action_id": {
                        "type": "string",
                        "description": "Optional pending action ID to approve and send.",
                    },
                    "company_name": {
                        "type": "string",
                        "description": "Optional company name to use in the email signature, if the recruiter provided one.",
                    },
                    "interviewer_name": {
                        "type": "string",
                        "description": "Optional interviewer or host name to use in the email, if the recruiter provided one.",
                    },
                    "omit_contact_info": {
                        "type": "boolean",
                        "description": "Set true if recruiter requested omitting phone/email in signoff",
                    },
                },
            },
        },
    },
]

TOOL_METADATA: dict[str, dict[str, str]] = {
    "search_candidates": {"label": "Searching talent database", "tool": "search_candidates"},
    "get_candidate_profile": {"label": "Loading candidate dossier", "tool": "candidate_profile"},
    "draft_candidate_email": {"label": "Drafting outbound communication", "tool": "draft_email"},
    "schedule_interview": {"label": "Scheduling interview & video meeting", "tool": "schedule_interview"},
    "approve_and_send_email": {"label": "Sending email via Gmail", "tool": "send_email"},
    "verify_candidate": {"label": "Verifying LeetCode & GitHub ratings", "tool": "verify_skills"},
    "update_candidate_status": {"label": "Updating candidate pipeline stage", "tool": "pipeline_update"},
    "list_interviews": {"label": "Reading interview calendar", "tool": "interview_calendar"},
    "get_pipeline_summary": {"label": "Gathering pipeline metrics", "tool": "pipeline_summary"},
}

SYSTEM_PROMPT = """You are an intelligent, proactive AI Recruiter Assistant powering an enterprise Recruitment Operating System.
You work alongside human recruiters to find top engineering talent, evaluate technical qualifications, draft professional communications, and coordinate interviews.

CRITICAL INSTRUCTIONS & RECRUITER POLICIES:
1. Multi-turn Conversational Memory:
   - Always remember the candidates and topics discussed in previous messages in this conversation.
   - When the recruiter says "him", "her", "them", or asks to schedule/draft without repeating the candidate's name, refer to the candidate discussed earlier in the conversation.
2. Email Drafting, Outreach & Immediate Sending:
   - When the user asks to draft an email (e.g. "Draft a mail for him" or "Draft a shortlist email"):
     - IMMEDIATELY call `draft_candidate_email` with a well-crafted, polished email body and subject line.
     - NEVER say "nobody is waiting on a follow-up" when the recruiter explicitly asked you to draft an email! Write the requested email.
     - CRITICAL PLACEHOLDER PREVENTION:
       - NEVER EVER draft or output unpopulated bracket placeholders such as [DATE], [TIME], [TIMEZONE], [MEETING_PROVIDER], [JOIN_LINK], [LINK], [Company], [Your Name], [Interviewer Name]!
       - If an interview has already been scheduled or discussed, use the EXACT date, time, timezone, platform, and actual live Microsoft Teams / Google Meet join URL returned by the scheduling tool — never invent one.
       - Note to the recruiter: Both the candidate (attendee) and the reviewer/interviewer (host) join using this exact same Microsoft Teams / Google Meet link.
     - In your response, clearly present the drafted email (Subject, Recipient, Body, and Interviewer/Candidate join links) and inform the user that it is queued for their approval.
     - If the recruiter did NOT specify an interview time:
       - You may draft an email asking for the candidate's availability OR
       - Ask the recruiter: "What date and time would you like to schedule the interview for (e.g., tomorrow at 3:00 PM)? Once you provide the time, I can create the Microsoft Teams/Google Meet invite, generate the join link, and embed it into the email."
   - When the user confirms to send the email (e.g. "send it", "no send it for now", "send it for now", "send the mail", "yes send it", "go ahead and send it", "approve and send", "send now", "send it again"):
     - IMMEDIATELY call `approve_and_send_email`.
     - If the recruiter supplied a company name, interviewer name, or asked to omit contact details, pass `company_name`, `interviewer_name`, and `omit_contact_info=true` accordingly. Never invent these values.
     - In your final response:
       - Clearly confirm that the invitation email has been sent to the candidate via your connected Gmail account. Mention the candidate's name, email address, company name, interviewer name, and meeting link.
       - NEVER say "I will not send the invitation yet", "it remains in draft status", or "waiting for your approval" after calling `approve_and_send_email`!
3. Meeting Scheduling & Video Meetings:
   - When asked to schedule an interview or create a meeting link (Teams or Google Meet):
     - If date and time is NOT provided: ASK the recruiter for their preferred date and time (e.g. "What date and time would you like to schedule the interview for?").
     - If date and time IS provided (e.g. "make a meeting link on the 12th of November at 4 pm"):
       - IMMEDIATELY call `schedule_interview` with candidate_name_or_id, time_expression, and preferred meeting_provider (microsoft_teams or google_meet).
       - In your final response:
         1. State clearly that the interview is confirmed and scheduled.
         2. Provide the exact date, time, and timezone (e.g. Thursday, 12 November 2026 at 4:00 PM IST).
         3. Provide the actual meeting join link as a prominent markdown link and bold URL.
         4. Clearly state that the reviewer/interviewer joins using this exact same meeting URL.
         5. Present the full drafted invitation email (Recipient, Subject, and Body) containing the join link, and confirm it is saved and queued for recruiter approval in the dashboard.
       - NEVER say "I can't create the live meeting link in this turn", "join link to be generated", or "placeholder". The tool creates and returns the live join link immediately!
     - Provider switching (CRITICAL):
       - If the recruiter says to switch providers — e.g. "switch to Google Meet", "go for Google Meet", "use Meet", "Google Meet please" — you MUST immediately call `schedule_interview` again for the SAME date/time with `meeting_provider="google_meet"`. Do NOT ask for confirmation again, do NOT keep retrying Microsoft Teams, and do NOT just list options in prose.
       - If a `schedule_interview` attempt fails with a Microsoft Teams error (authentication, token, or not-connected), do NOT retry Teams in that same turn. Report the failure once and, when the recruiter has asked for Google Meet or agreed to it, call `schedule_interview` with `meeting_provider="google_meet"` immediately.
       - Only ever report a join link that the tool actually returned. Never claim a Teams or Meet link was generated when the tool returned an error, and never invent a link or a calendar-invite placeholder.
       - After switching, state clearly that the platform is now Google Meet and include the real join link returned by the tool.
     - App connections (Composio):
       - If an external app (Gmail, Google Calendar, Google Meet, Microsoft Teams, GitHub) is not connected, call `COMPOSIO_MANAGE_CONNECTIONS` for that toolkit and share the returned Connect Link with the recruiter. Never say you cannot connect an account, and never ask the recruiter to paste credentials.
       - When you need an app action that is not covered by a recruiter tool above, use `COMPOSIO_SEARCH_TOOLS` to find the right tool and `COMPOSIO_MULTI_EXECUTE_TOOL` to run it. Never fabricate a result or a link.
4. Technical Verification:
   - Use `verify_candidate` to look up LeetCode ratings, problem counts, and GitHub repositories.
   - If the recruiter provides a LeetCode or GitHub profile URL, pass it to `verify_candidate` as `leetcode_url` / `github_url`. Never report a profile as "not verified" when the recruiter just gave you a URL — call the tool with that URL first.
   - If the recruiter provides a personal website or portfolio URL, or asks you to check/inspect the candidate's portfolio (even a URL in the message), pass it as `profile_url`. The tool fetches the live page and returns `website.text`, `website.headings`, `website.links` and any discovered GitHub/LeetCode/LinkedIn profiles. Summarise the projects, skills, and claims actually on that page, and verify the profiles it links to.
   - You CAN read websites through this tool. NEVER say "I can't browse the live site" or ask the recruiter to paste the content — call `verify_candidate` with `profile_url` instead.
5. Proactive, Executive Assistance:
   - Be professional, concise, proactive, and clear. Always outline next steps.
"""


async def _resolve_candidate(
    session: AsyncSession, workspace_id: str, name_or_id: str | None
) -> dict[str, Any] | None:
    if not name_or_id:
        # Fallback to most recent candidate in workspace
        row = (
            await session.execute(
                sa.select(candidates)
                .where(candidates.c.workspace_id == workspace_id)
                .order_by(candidates.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
        return dict(row) if row else None

    # Check by UUID
    try:
        row = (
            await session.execute(
                sa.select(candidates).where(
                    candidates.c.workspace_id == workspace_id,
                    candidates.c.id == name_or_id,
                )
            )
        ).mappings().first()
        if row:
            return dict(row)
    except Exception:
        pass

    # Check by Name
    clean_name = name_or_id.strip().lower()
    row = (
        await session.execute(
            sa.select(candidates)
            .where(
                candidates.c.workspace_id == workspace_id,
                sa.func.lower(candidates.c.full_name).like(f"%{clean_name}%"),
            )
            .order_by(candidates.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()
    if row:
        return dict(row)

    # Fallback to first candidate
    row = (
        await session.execute(
            sa.select(candidates)
            .where(candidates.c.workspace_id == workspace_id)
            .order_by(candidates.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


# --------------------------------------------------------------------- Email Hydrator
async def _hydrate_email_content(
    session: AsyncSession,
    workspace_id: str,
    cand: dict[str, Any],
    recruiter_id: str | None,
    subject: str,
    body: str,
    company_name: str | None = None,
    interviewer_name: str | None = None,
    omit_contact: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    """Plan a draft's placeholders via the shared hydrator.

    Delegates to :func:`services.communications.hydrate_placeholders` so the
    assistant draft path and the send-time safety net share one implementation.
    """
    sender_email = await interview_svc._recruiter_email(session, workspace_id, recruiter_id)
    h_subj, h_body, info = await comms.hydrate_placeholders(
        session,
        workspace_id=workspace_id,
        candidate=cand,
        subject=subject,
        body=body,
        company_name=company_name,
        interviewer_name=interviewer_name,
        sender_email=sender_email,
        omit_contact=omit_contact,
    )
    role_name = cand.get("current_role")
    if role_name and "the role" in h_subj.lower():
        h_subj = h_subj.replace("the role", role_name).replace("The Role", role_name)
    return h_subj, h_body, info


# --------------------------------------------------------------------- Tool Executors
_MEET_SWITCH_RE = re.compile(r"google\s*meet|gmeet|meet\.google\.com", re.IGNORECASE)
_TEAMS_SWITCH_RE = re.compile(r"microsoft\s*teams|ms\s*teams|\bteams\b", re.IGNORECASE)


def provider_hint_from_message(message: str | None) -> str | None:
    """Best-effort provider override from the recruiter's latest message.

    Only used when the model omits ``meeting_provider``. Google Meet is checked
    first so an explicit "switch to Google Meet" wins even if Teams is also
    mentioned (e.g. "Teams failed, use Google Meet").
    """
    text = (message or "").strip()
    if not text:
        return None
    if _MEET_SWITCH_RE.search(text):
        return "google_meet"
    if _TEAMS_SWITCH_RE.search(text):
        return "microsoft_teams"
    return None


async def execute_tool_call(
    *,
    name: str,
    args: dict[str, Any],
    session: AsyncSession,
    workspace_id: str,
    recruiter_id: str | None,
    accumulated_candidates: list[dict[str, Any]],
    accumulated_pending: list[dict[str, Any]],
) -> dict[str, Any]:
    if name == "search_candidates":
        query = (args.get("query") or "").strip()
        skills = args.get("skills") or []
        location = args.get("location")
        min_exp = args.get("min_experience_years")
        status = args.get("status")

        filters: dict[str, Any] = {}
        if skills:
            filters["skills"] = skills
        if location:
            filters["location"] = location
        if min_exp:
            filters["min_experience_years"] = min_exp
        if status:
            filters["pipeline_status"] = [status]

        rows = await list_candidates(session, workspace_id, filters=filters, limit=10)
        if not rows:
            # Broad search across name, role, skills
            stmt = sa.select(candidates).where(candidates.c.workspace_id == workspace_id).limit(10)
            rows = [dict(r) for r in (await session.execute(stmt)).mappings().all()]

        summaries = [candidate_summary(r) for r in rows]
        accumulated_candidates.extend(summaries)
        return {
            "found_count": len(rows),
            "candidates": [
                {
                    "id": r["id"],
                    "name": r["full_name"],
                    "current_role": r.get("current_role"),
                    "skills": r.get("skills", []),
                    "experience_years": r.get("experience_years"),
                    "location": r.get("location"),
                    "pipeline_status": r.get("pipeline_status"),
                }
                for r in rows
            ],
        }

    elif name == "get_candidate_profile":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        if not cand:
            return {"error": "Candidate not found."}
        cand_sum = candidate_summary(cand)
        accumulated_candidates.append(cand_sum)
        return {
            "candidate_id": cand["id"],
            "full_name": cand["full_name"],
            "email": cand.get("email"),
            "current_role": cand.get("current_role"),
            "skills": cand.get("skills", []),
            "experience_years": cand.get("experience_years"),
            "pipeline_status": cand.get("pipeline_status"),
            "summary": cand.get("summary"),
            "github_url": cand.get("github_url"),
            "leetcode_url": cand.get("leetcode_url"),
        }

    elif name == "draft_candidate_email":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        if not cand:
            return {"error": "Candidate not found."}

        raw_subject = args.get("subject", "Update regarding your application")
        raw_body = args.get("body", "")
        email_type = args.get("email_type", "shortlist")

        subject, body, int_info = await _hydrate_email_content(
            session, workspace_id, cand, recruiter_id, raw_subject, raw_body
        )

        comm = await comms.queue_email(
            session,
            workspace_id=workspace_id,
            candidate=cand,
            recruiter_id=recruiter_id,
            kind=email_type,
            subject=subject,
            body=body,
            method="gpt-5-nano",
        )
        action = await create_pending_action(
            session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            kind="send_email",
            title=f"Send {email_type.replace('_', ' ')} email to {cand['full_name']}",
            payload={"communication_id": comm["id"], "candidate_id": cand["id"], "subject": subject},
        )

        # Move to SHORTLISTED if shortlist email
        if email_type == "shortlist" and cand.get("pipeline_status") != "SHORTLISTED":
            await set_candidate_status(
                session,
                workspace_id=workspace_id,
                candidate_id=cand["id"],
                to_status="SHORTLISTED",
                changed_by=recruiter_id,
                reason="Candidate shortlisted via AI Assistant",
            )

        await session.commit()
        accumulated_pending.append(action)
        return {
            "status": "queued_for_approval",
            "communication_id": comm["id"],
            "candidate_name": cand["full_name"],
            "candidate_email": cand.get("email"),
            "subject": subject,
            "body": body,
            "meeting_details": int_info,
            "message": f"Email drafted for {cand['full_name']} and queued for your approval.",
        }

    elif name == "schedule_interview":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        if not cand:
            return {"error": "Candidate not found."}

        time_str = args.get("time_expression")
        when = svc.parse_when(time_str, timezone_name="Asia/Kolkata")
        if not when:
            return {"error": f"Could not parse '{time_str}'. Ask the recruiter for a specific date and time."}

        duration = int(args.get("duration_minutes") or 30)
        # An explicit provider from the recruiter always wins (this is what makes
        # "switch to Google Meet" actually switch). When it is omitted we use the
        # workspace setting instead of hard-coding Teams, so a workspace configured
        # for Google Meet is never silently forced back onto Teams.
        raw_provider = (args.get("meeting_provider") or "").strip()
        if raw_provider:
            provider = normalize_meeting_provider(raw_provider)
        else:
            provider = await meeting_provider_for(session, workspace_id)
        if provider == "none":
            provider = "microsoft_teams"

        payload = {
            "candidate_id": cand["id"],
            "candidate_name": cand.get("full_name"),
            "start": when.astimezone(timezone.utc).isoformat(),
            "duration_minutes": duration,
            "timezone": "Asia/Kolkata",
            "provider": provider,
            "title": args.get("title") or f"Interview: {cand['full_name']}",
            "notes": args.get("notes") or "Initial technical interview",
        }

        try:
            res = await interview_svc.execute_schedule(
                session,
                workspace_id=workspace_id,
                recruiter_id=recruiter_id,
                payload=payload,
            )
        except IntegrationError as exc:
            return {"error": f"Meeting provider error: {exc}"}

        pending_act = res.get("confirmation_action") or res.get("action")
        interview_id = res.get("interview_id") or (res.get("interview") or {}).get("id")
        meeting_url = res.get("meeting_url") or (res.get("interview") or {}).get("meeting_url")

        if pending_act:
            accumulated_pending.append(pending_act)

        comm = res.get("confirmation_email") or res.get("communication") or {}
        email_body = comm.get("content") or comm.get("body") or ""
        email_subject = comm.get("subject") or f"Interview Scheduled — {cand['full_name']}"

        if not email_body and interview_id:
            comm_row = (
                await session.execute(
                    sa.select(candidate_communications)
                    .where(candidate_communications.c.candidate_id == cand["id"])
                    .order_by(candidate_communications.c.created_at.desc())
                    .limit(1)
                )
            ).mappings().first()
            if comm_row:
                email_body = comm_row.get("content") or ""
                email_subject = comm_row.get("subject") or email_subject

        formatted_time = when.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%A, %d %B %Y at %I:%M %p %Z")
        display_provider = "Google Meet" if provider == "google_meet" else "Microsoft Teams"

        return {
            "status": res.get("status") or "scheduled",
            "candidate_name": cand["full_name"],
            "candidate_email": cand.get("email"),
            "scheduled_time": formatted_time,
            "meeting_url": meeting_url,
            "provider": display_provider,
            "interview_id": interview_id,
            "email_draft": {
                "subject": email_subject,
                "body": email_body,
                "recipient": cand.get("email"),
            },
            "pending_action_id": pending_act.get("id") if pending_act else None,
            "message": (
                f"Interview successfully scheduled for {cand['full_name']} on {formatted_time}. "
                f"Video Meeting Link ({display_provider}): {meeting_url}. "
                f"Candidate invitation email has been drafted with this link and queued for recruiter approval."
            ),
        }

    elif name == "approve_and_send_email":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        action_id = args.get("action_id")
        company_arg = args.get("company_name")
        interviewer_arg = args.get("interviewer_name")
        omit_contact = bool(args.get("omit_contact_info"))

        target_action = None
        if action_id:
            target_action = (
                await session.execute(
                    sa.select(pending_actions).where(
                        pending_actions.c.id == action_id,
                        pending_actions.c.workspace_id == workspace_id,
                        pending_actions.c.status == "pending",
                    )
                )
            ).mappings().first()

        if not target_action and cand:
            # Only ever approve a pending action that belongs to this candidate —
            # never "any pending action in the workspace" (that could send the
            # wrong candidate's email).
            recent_pending = (
                await session.execute(
                    sa.select(pending_actions)
                    .where(
                        pending_actions.c.workspace_id == workspace_id,
                        pending_actions.c.status == "pending",
                    )
                    .order_by(pending_actions.c.created_at.desc())
                )
            ).mappings().all()
            for pa in recent_pending:
                payload = pa.get("payload") or {}
                if str(payload.get("candidate_id")) == str(cand["id"]):
                    target_action = pa
                    break

        if not target_action:
            return {
                "status": "nothing_to_send",
                "error": "There is no pending email to approve. Draft the email first, then approve sending it.",
            }

        # Apply explicit company/interviewer/contact overrides to the queued draft.
        comm_id = (target_action.get("payload") or {}).get("communication_id")
        if comm_id and cand and (company_arg or interviewer_arg or omit_contact):
            comm_row = (
                await session.execute(
                    sa.select(candidate_communications).where(candidate_communications.c.id == comm_id)
                )
            ).mappings().first()
            if comm_row:
                h_subj, h_body, _ = await _hydrate_email_content(
                    session,
                    workspace_id,
                    cand,
                    recruiter_id,
                    comm_row["subject"] or "",
                    comm_row["content"] or "",
                    company_name=company_arg,
                    interviewer_name=interviewer_arg,
                    omit_contact=omit_contact,
                )
                await session.execute(
                    sa.update(candidate_communications)
                    .where(candidate_communications.c.id == comm_id)
                    .values(content=h_body, subject=h_subj)
                )
                await session.commit()

        try:
            exec_res = await interview_svc.execute_pending_action(
                session,
                workspace_id=workspace_id,
                action_id=target_action["id"],
                recruiter_id=recruiter_id,
            )
        except Exception as exc:
            return {"error": f"Failed to send email: {exc}"}

        accumulated_pending[:] = [a for a in accumulated_pending if a.get("id") != target_action["id"]]
        recipient_email = (cand or {}).get("email")
        candidate_name = (cand or {}).get("full_name") or "the candidate"
        return {
            "status": "sent",
            "action_id": target_action["id"],
            "candidate_name": candidate_name,
            "recipient": recipient_email,
            "provider_message_id": (exec_res.get("result") or {}).get("provider_message_id"),
            "message": f"Email sent to {candidate_name} ({recipient_email}) via your connected Gmail account.",
        }

    elif name == "verify_candidate":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        if not cand:
            return {"error": "Candidate not found."}

        # A recruiter may paste profile URLs in chat. Validate them, persist them
        # on the candidate (so later runs and the dashboard see them), and verify
        # against the merged record rather than the stale one — otherwise the
        # URLs are silently ignored and LeetCode/GitHub always reports "not
        # verified".
        updates: dict[str, Any] = {}
        github_arg = verification.profile_links.normalize_url(args.get("github_url")) or ""
        leetcode_arg = verification.profile_links.normalize_url(args.get("leetcode_url")) or ""
        profile_arg = verification.profile_links.normalize_url(args.get("profile_url")) or ""
        if (
            github_arg
            and verification.parse_username(github_arg, "github.com")
            and github_arg != cand.get("github_url")
        ):
            updates["github_url"] = github_arg
        if (
            leetcode_arg
            and verification.parse_username(leetcode_arg, "leetcode.com")
            and leetcode_arg != cand.get("leetcode_url")
        ):
            updates["leetcode_url"] = leetcode_arg
        if profile_arg and verification.profile_links.is_public_url(profile_arg):
            kind = verification.profile_links.classify_url(profile_arg)
            if kind == "github" and not updates.get("github_url"):
                updates["github_url"] = profile_arg
            elif kind == "leetcode" and not updates.get("leetcode_url"):
                updates["leetcode_url"] = profile_arg
            elif kind == "website" and profile_arg != cand.get("portfolio_url"):
                updates["portfolio_url"] = profile_arg
        if updates:
            await session.execute(
                sa.update(candidates)
                .where(candidates.c.id == cand["id"])
                .values(**updates, updated_at=utcnow())
            )
            await session.commit()
            cand = {**cand, **updates}

        # enrich=True: if GitHub/LeetCode are still missing, crawl the personal
        # website once and mine GitHub/LeetCode/LinkedIn links from it.
        v = await verification.verify_candidate(
            cand, keywords=list(cand.get("skills") or []), enrich=True
        )
        gh = v.get("github") or {}
        lc = v.get("leetcode") or {}
        website = v.get("website")
        discovered = v.get("discovered") or {}

        # Persist anything the crawl discovered so the dashboard and later runs
        # keep the richer profile.
        learned: dict[str, Any] = {
            field: value
            for field, value in (
                ("github_url", discovered.get("github_url")),
                ("leetcode_url", discovered.get("leetcode_url")),
                ("linkedin_url", discovered.get("linkedin_url")),
            )
            if value and value != cand.get(field)
        }
        if learned:
            await session.execute(
                sa.update(candidates)
                .where(candidates.c.id == cand["id"])
                .values(**learned, updated_at=utcnow())
            )
            await session.commit()

        result: dict[str, Any] = {
            "candidate_name": cand["full_name"],
            "evidence_score": v.get("evidence_score"),
            "signals": v.get("signals") or [],
            "github": {
                "available": gh.get("available", False),
                "reason": gh.get("reason"),
                "profile_url": gh.get("profile_url") or gh.get("source_url"),
                "username": gh.get("username"),
                "public_repos": gh.get("public_repos"),
                "relevant_repos": [r.get("name") for r in (gh.get("relevant_repositories") or [])[:5]],
                "discovered_from": gh.get("discovered_from"),
            },
            "leetcode": {
                "available": lc.get("available", False),
                "reason": lc.get("reason"),
                "profile_url": lc.get("profile_url") or lc.get("source_url"),
                "username": lc.get("username"),
                "ranking": lc.get("ranking"),
                "total_solved": lc.get("total_solved"),
                "solved_by_difficulty": lc.get("solved_by_difficulty") or {},
                "contest_rating": lc.get("contest_rating"),
                "discovered_from": lc.get("discovered_from"),
            },
        }
        if website is not None:
            result["website"] = {
                "available": website.get("available", False),
                "reason": website.get("reason"),
                "url": website.get("final_url") or cand.get("portfolio_url"),
                "title": website.get("title"),
                "description": website.get("description"),
                "headings": (website.get("headings") or [])[:40],
                "text": website.get("text"),
                "discovered": discovered,
                "links": {
                    kind: (website.get("links") or {}).get(kind, [])[:5]
                    for kind in ("github", "leetcode", "linkedin", "website")
                },
            }
        return result

    elif name == "update_candidate_status":
        cand = await _resolve_candidate(session, workspace_id, args.get("candidate_name_or_id"))
        if not cand:
            return {"error": "Candidate not found."}

        new_status = args.get("status")
        prev = await set_candidate_status(
            session,
            workspace_id=workspace_id,
            candidate_id=cand["id"],
            to_status=new_status,
            changed_by=recruiter_id,
            reason="Updated via AI Assistant",
        )
        await session.commit()
        return {"candidate_name": cand["full_name"], "previous_status": prev, "new_status": new_status}

    elif name == "list_interviews":
        rows = await upcoming_interviews(session, workspace_id, limit=20)
        return {
            "count": len(rows),
            "interviews": [
                {
                    "candidate_name": r.get("candidate_name"),
                    "start_time": r.get("start_time").isoformat() if r.get("start_time") else None,
                    "provider": r.get("provider"),
                    "status": r.get("status"),
                    "meeting_url": r.get("meeting_url"),
                }
                for r in rows
            ],
        }

    elif name == "get_pipeline_summary":
        now = utcnow()
        counts_q = sa.select(
            sa.func.count().label("total"),
            sa.func.count().filter(candidates.c.pipeline_status.in_(("APPLIED", "SCREENED"))).label("awaiting"),
            sa.func.count().filter(candidates.c.pipeline_status == "SHORTLISTED").label("shortlisted"),
        ).where(candidates.c.workspace_id == workspace_id)
        row = (await session.execute(counts_q)).mappings().one_or_none() or {}
        return {
            "total_candidates": int(row.get("total") or 0),
            "awaiting_screening": int(row.get("awaiting") or 0),
            "shortlisted": int(row.get("shortlisted") or 0),
        }

    return {"status": "ok", "message": f"Action {name} processed."}


# --------------------------------------------------------------------- Agent (Composio + LangChain)
COMPOSIO_TOOLKITS = ["gmail", "googlecalendar", "googlemeet", "microsoft_teams", "github"]

# Pretty names for the "connect this app" prompt in the chat UI.
TOOLKIT_NAMES: dict[str, str] = {
    "gmail": "Gmail",
    "github": "GitHub",
    "googlecalendar": "Google Calendar",
    "googlemeet": "Google Meet",
    "microsoft_teams": "Microsoft Teams",
}


def _toolkit_name(slug: str) -> str:
    return TOOLKIT_NAMES.get(slug, slug.replace("_", " ").title())


def _toolkit_logo(slug: str) -> str:
    # Public Composio logo endpoint (no auth required), stable per toolkit slug.
    return f"https://logos.composio.dev/api/{slug}"


# Cache the session *and* its LangChain tools per workspace. Session creation is
# a network round-trip, so we keep it warm for the same 25 minutes the MCP path
# uses. The session is needed to read connection status; the tools close over it.
_COMPOSIO_CACHE: dict[str, tuple[float, Any, list[Any]]] = {}

_COMPOSIO_LABELS: dict[str, tuple[str, str]] = {
    "COMPOSIO_SEARCH_TOOLS": ("composio", "Finding the right app tool"),
    "COMPOSIO_GET_TOOL_SCHEMAS": ("composio", "Inspecting a tool schema"),
    "COMPOSIO_MULTI_EXECUTE_TOOL": ("composio", "Running an app action"),
    "COMPOSIO_MANAGE_CONNECTIONS": ("composio", "Connecting an app"),
    "COMPOSIO_REMOTE_WORKBENCH": ("composio", "Remote workbench"),
    "COMPOSIO_REMOTE_BASH_TOOL": ("composio", "Remote shell"),
}


def _composio_user_id(workspace_id: str) -> str:
    return f"{settings.composio_user_prefix}:{workspace_id}"


def _load_composio_session(workspace_id: str) -> tuple[Any, list[Any]]:
    """Blocking: create a Composio session and return ``(session, tools)``.

    Exactly the pattern from the Composio LangChain docs:

        Composio(provider=LangchainProvider()).create(user_id=..., toolkits=[...]).tools()

    The session exposes meta-tools (search / manage connections / execute), so
    there is no per-toolkit boilerplate to maintain.
    """
    if not settings.composio_enabled:
        return None, []
    kwargs: dict[str, Any] = {
        "api_key": settings.composio_api_key,
        "provider": LangchainProvider(),
    }
    if settings.composio_base_url:
        kwargs["base_url"] = settings.composio_base_url.rstrip("/")
    session = Composio(**kwargs).create(
        user_id=_composio_user_id(workspace_id),
        toolkits=COMPOSIO_TOOLKITS,
    )
    return session, list(session.tools())


async def _composio(workspace_id: str) -> tuple[Any | None, list[Any]]:
    cached = _COMPOSIO_CACHE.get(workspace_id)
    if cached and (time.time() - cached[0]) < 25 * 60:
        return cached[1], cached[2]
    try:
        session, tools = await asyncio.to_thread(_load_composio_session, workspace_id)
    except Exception as exc:  # noqa: BLE001 - never block a chat turn on Composio
        log.warning("composio session unavailable: %s", exc)
        return None, []
    _COMPOSIO_CACHE[workspace_id] = (time.time(), session, tools)
    return session, tools


def _connection_status(session: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Blocking: read Composio connection state for the session's toolkits.

    Returns ``(connected_slugs, missing)`` where each missing entry is a
    ``{"slug", "name", "logo"}`` dict ready for the frontend prompt.
    """
    try:
        details = session.toolkits()
        items = getattr(details, "items", None) or []
    except Exception as exc:  # noqa: BLE001 - status is best-effort
        log.warning("composio toolkit status unavailable: %s", exc)
        return [], []
    connected: list[str] = []
    missing: list[dict[str, Any]] = []
    for item in items:
        slug = getattr(item, "slug", None)
        if not slug:
            continue
        active = bool(getattr(getattr(item, "connection", None), "is_active", False))
        if active:
            connected.append(slug)
        else:
            missing.append({"slug": slug, "name": _toolkit_name(slug), "logo": _toolkit_logo(slug)})
    return connected, missing


def _connect_links_from(output: Any) -> list[dict[str, Any]]:
    """Extract ``{toolkit, name, logo, url}`` from a COMPOSIO_MANAGE_CONNECTIONS result.

    Composio returns::

        {"data": {"results": {"github": {"toolkit": "github",
                                          "status": "initiated",
                                          "redirect_url": "https://connect..."}}}}

    We only surface entries that actually carry a ``redirect_url``.
    """
    raw = getattr(output, "content", None)
    if raw is None:
        raw = output
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001 - not JSON, nothing to surface
            return []
    if not isinstance(raw, dict):
        return []
    results = (raw.get("data") or {}).get("results")
    links: list[dict[str, Any]] = []
    if isinstance(results, dict):
        for slug, info in results.items():
            if isinstance(info, dict) and info.get("redirect_url"):
                links.append(
                    {
                        "toolkit": slug,
                        "name": _toolkit_name(slug),
                        "logo": _toolkit_logo(slug),
                        "url": info["redirect_url"],
                    }
                )
    return links


def _domain_tools(
    *,
    session: AsyncSession,
    workspace_id: str,
    recruiter_id: str | None,
    user_message: str,
    accumulated_candidates: list[dict[str, Any]],
    accumulated_pending: list[dict[str, Any]],
) -> list[Any]:
    """Expose the recruiter actions to LangChain.

    Reuses the existing ``AGENT_TOOLS`` schemas and ``execute_tool_call``
    dispatcher, so candidate search, verification, scheduling and approvals keep
    working with no rewrite. Each OpenAI schema is converted to a pydantic model
    and wrapped as a LangChain ``StructuredTool``.
    """
    tools: list[Any] = []
    for spec in AGENT_TOOLS:
        fn = spec["function"]
        name = fn["name"]
        schema = dict(fn.get("parameters") or {"type": "object", "properties": {}})
        schema.setdefault("title", name)
        args_schema = _schema_model(schema)

        async def _run(*, _name: str = name, **kwargs: Any) -> str:
            # Deterministic safety net: if the model forgets the provider on a
            # switch request, honour the recruiter's latest message.
            if _name == "schedule_interview" and not (kwargs.get("meeting_provider") or "").strip():
                hint = provider_hint_from_message(user_message)
                if hint:
                    kwargs["meeting_provider"] = hint
            result = await execute_tool_call(
                name=_name,
                args=kwargs,
                session=session,
                workspace_id=workspace_id,
                recruiter_id=recruiter_id,
                accumulated_candidates=accumulated_candidates,
                accumulated_pending=accumulated_pending,
            )
            if not isinstance(result, dict):
                result = {"status": "ok", "message": str(result)}
            return json.dumps(result, default=str)

        tools.append(
            StructuredTool.from_function(
                coroutine=_run,
                name=name,
                description=fn.get("description", ""),
                args_schema=args_schema,
            )
        )
    return tools


def _build_agent(tools: list[Any], *, system_prompt: str = SYSTEM_PROMPT) -> Any:
    model = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.llm_temperature,
    )
    return create_agent(model=model, tools=tools, system_prompt=system_prompt)


# --------------------------------------------------------------------- Streaming Turn Runner
async def run_assistant_stream(
    *,
    session: AsyncSession,
    workspace_id: str,
    recruiter_id: str | None,
    thread_id: str,
    user_message: str,
) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "meta", "thread_id": thread_id}

    # Ensure thread exists in chat_threads
    existing_th = (
        await session.execute(sa.select(chat_threads).where(chat_threads.c.id == thread_id))
    ).mappings().first()

    if not existing_th:
        now = utcnow()
        title = user_message[:60].strip() or "New Conversation"
        await session.execute(
            sa.insert(chat_threads).values(
                id=thread_id,
                workspace_id=workspace_id,
                title=title,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    # 1. Save user message to database
    await session.execute(
        sa.insert(chat_messages).values(
            id=db_module.new_id(),
            workspace_id=workspace_id,
            thread_id=thread_id,
            role="user",
            content=user_message,
            payload={},
            created_at=utcnow(),
        )
    )
    await session.commit()

    # 2. Load previous conversation history for this thread
    prior_rows = (
        await session.execute(
            sa.select(chat_messages)
            .where(
                chat_messages.c.thread_id == thread_id,
                chat_messages.c.workspace_id == workspace_id,
            )
            .order_by(chat_messages.c.created_at.asc())
            .limit(16)
        )
    ).mappings().all()

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for row in prior_rows:
        r = row["role"]
        c = row["content"]
        if r in ("user", "assistant") and c:
            messages.append({"role": r, "content": c})

    accumulated_candidates: list[dict[str, Any]] = []
    accumulated_pending: list[dict[str, Any]] = []
    accumulated_connections: list[dict[str, Any]] = []
    seen_connections: set[str] = set()
    final_reply: str = ""

    # Step indicator for the agent turn
    yield {
        "type": "step",
        "id": "understand",
        "tool": settings.llm_model,
        "label": "Thinking with the recruiting agent",
        "status": "running",
        "detail": user_message[:120],
    }

    try:
        composio_session, composio_tools = await _composio(workspace_id)
        system_prompt = SYSTEM_PROMPT
        if composio_session is not None:
            connected, missing = await asyncio.to_thread(_connection_status, composio_session)
            if missing:
                missing_names = ", ".join(item["name"] for item in missing)
                system_prompt = (
                    SYSTEM_PROMPT
                    + "\n\nAPP CONNECTIONS (Composio):\n"
                    + f"- Connected: {', '.join(_toolkit_name(s) for s in connected) or 'none'}\n"
                    + f"- Not connected: {missing_names}\n"
                    + "If the recruiter's request needs a not-connected app, call "
                    + "`COMPOSIO_MANAGE_CONNECTIONS` with that app's toolkit slug, then tell the "
                    + "recruiter to click the connect link to finish. Never claim a not-connected "
                    + "app action succeeded."
                )

        tools = _domain_tools(
            session=session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            user_message=user_message,
            accumulated_candidates=accumulated_candidates,
            accumulated_pending=accumulated_pending,
        ) + composio_tools
        agent = _build_agent(tools, system_prompt=system_prompt)

        # ``messages`` still starts with the system prompt; create_agent owns it.
        lc_messages = [
            HumanMessage(content=row["content"]) if row["role"] == "user" else AIMessage(content=row["content"])
            for row in messages[1:]
        ]

        yield {
            "type": "step",
            "id": "understand",
            "tool": settings.llm_model,
            "label": "Thinking with the recruiting agent",
            "status": "done",
            "summary": f"{len(tools)} tools ready",
            "ms": 1,
        }

        started_at: dict[str, float] = {}
        streamed = False

        async for event in agent.astream_events({"messages": lc_messages}, version="v2"):
            kind = event.get("event")
            run_id = str(event.get("run_id") or "")
            name = event.get("name") or ""

            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                text = getattr(chunk, "content", None)
                if isinstance(text, list):
                    text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part) for part in text
                    )
                if text:
                    streamed = True
                    final_reply += text
                    yield {"type": "token", "text": text}

            elif kind == "on_chat_model_end" and not streamed:
                # Some OpenAI-compatible proxies do not stream tokens; keep the
                # final answer from the model-end event in that case.
                out = event.get("data", {}).get("output")
                content = getattr(out, "content", None)
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
                    )
                if content:
                    final_reply = str(content)

            elif kind == "on_tool_start":
                started_at[run_id] = time.perf_counter()
                meta = TOOL_METADATA.get(name)
                if meta:
                    tool_key, label = meta["tool"], meta["label"]
                else:
                    tool_key, label = _COMPOSIO_LABELS.get(
                        name, (name.lower(), name.replace("_", " ").title())
                    )
                detail = ""
                tool_input = event.get("data", {}).get("input")
                if isinstance(tool_input, dict):
                    detail = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:2])
                yield {
                    "type": "step",
                    "id": run_id,
                    "tool": tool_key,
                    "label": label,
                    "status": "running",
                    "detail": str(detail)[:100],
                }

            elif kind == "on_tool_end":
                meta = TOOL_METADATA.get(name)
                if meta:
                    tool_key, label = meta["tool"], meta["label"]
                else:
                    tool_key, label = _COMPOSIO_LABELS.get(
                        name, (name.lower(), name.replace("_", " ").title())
                    )
                elapsed_ms = int(
                    (time.perf_counter() - started_at.get(run_id, time.perf_counter())) * 1000
                )
                output = event.get("data", {}).get("output")
                summary = getattr(output, "content", None) or output
                yield {
                    "type": "step",
                    "id": run_id,
                    "tool": tool_key,
                    "label": label,
                    "status": "done",
                    "summary": str(summary)[:120],
                    "ms": elapsed_ms,
                }

                # Composio initiated an app connection: surface a "connect this
                # app" prompt in the chat UI instead of leaving it in prose.
                if name == "COMPOSIO_MANAGE_CONNECTIONS":
                    for link in _connect_links_from(output):
                        if link["toolkit"] in seen_connections:
                            continue
                        seen_connections.add(link["toolkit"])
                        accumulated_connections.append(link)
                        yield {"type": "connect", **link}

        if not final_reply:
            final_reply = "I processed your request."

    except Exception as exc:
        log.exception("Assistant agent execution failed")
        final_reply = f"I encountered an issue processing your request: {exc}"
        yield {
            "type": "step",
            "id": "understand",
            "tool": settings.llm_model,
            "label": "Thinking with the recruiting agent",
            "status": "error",
            "summary": str(exc)[:120],
            "ms": 0,
        }

    # 4. Save assistant message with structured payload
    payload = to_jsonable({
        "intent": "conversational_turn",
        "data": {},
        "candidates": accumulated_candidates,
        "pending_actions": accumulated_pending,
        "connections": accumulated_connections,
    })

    await session.execute(
        sa.insert(chat_messages).values(
            id=db_module.new_id(),
            workspace_id=workspace_id,
            thread_id=thread_id,
            role="assistant",
            content=final_reply,
            payload=payload,
            created_at=utcnow(),
        )
    )
    await session.execute(
        sa.update(chat_threads).where(chat_threads.c.id == thread_id).values(updated_at=utcnow())
    )
    await session.commit()

    yield {
        "type": "done",
        "thread_id": thread_id,
        "message": final_reply,
        "reply": final_reply,
        "candidates": accumulated_candidates,
        "pending_actions": accumulated_pending,
        "connections": accumulated_connections,
    }


# --------------------------------------------------------------------- Synchronous Runner
async def run_assistant(
    *,
    session: AsyncSession,
    workspace_id: str,
    recruiter_id: str | None,
    thread_id: str,
    user_message: str,
) -> dict[str, Any]:
    final_payload: dict[str, Any] = {}
    async for event in run_assistant_stream(
        session=session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        thread_id=thread_id,
        user_message=user_message,
    ):
        if event.get("type") == "done":
            final_payload = event
    return final_payload
