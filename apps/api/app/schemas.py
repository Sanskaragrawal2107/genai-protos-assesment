"""Pydantic contracts shared by the API, the LangGraph workflows and the UI."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PipelineStatus = Literal[
    "APPLIED",
    "SCREENED",
    "SHORTLISTED",
    "CONTACTED",
    "INTERVIEW_SCHEDULED",
    "INTERVIEWED",
    "SELECTED",
    "REJECTED",
    "ON_HOLD",
]

MeetingProvider = Literal["microsoft_teams", "google_meet", "none"]
# Connector providers the UI can request. ``googlecalendar``/``googlemeet`` are
# the raw Composio toolkit slugs and are accepted as aliases.
ConnectorProvider = Literal[
    "gmail",
    "microsoft_teams",
    "google_calendar",
    "google_meet",
    "googlecalendar",
    "googlemeet",
]


# --------------------------------------------------------------- extraction
class ResumeProject(BaseModel):
    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None


class ResumeEducation(BaseModel):
    degree: str | None = None
    institution: str | None = None
    graduation_year: int | None = None
    field: str | None = None


class ResumeExtraction(BaseModel):
    """Strict LLM output schema for a resume. Missing data must be null."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    preferred_location: str | None = None
    experience_years: float | None = None
    current_role: str | None = None
    current_company: str | None = None
    previous_companies: list[str] = Field(default_factory=list)
    education: list[ResumeEducation] = Field(default_factory=list)
    university: str | None = None
    graduation_year: int | None = None
    skills: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    summary: str | None = None
    github_url: str | None = None
    leetcode_url: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None

    @field_validator(
        "previous_companies",
        "education",
        "skills",
        "programming_languages",
        "frameworks",
        "tools",
        "projects",
        mode="before",
    )
    @classmethod
    def _coerce_resume_lists(cls, v: Any) -> list:
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    @field_validator("experience_years")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is None:
            return None
        return max(0.0, min(float(v), 60.0))


# ------------------------------------------------------------------- jobs
class JobRequirements(BaseModel):
    title: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    min_experience_years: float | None = None
    preferred_experience_years: float | None = None
    required_location: str | None = None
    locations: list[str] = Field(default_factory=list)
    education: str | None = None
    required_projects: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    qualifications: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)

    @field_validator(
        "required_skills",
        "preferred_skills",
        "locations",
        "required_projects",
        "responsibilities",
        "qualifications",
        "keywords",
        mode="before",
    )
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
        return [str(v)]

    @field_validator("weights", mode="before")
    @classmethod
    def _coerce_weights(cls, v: Any) -> dict[str, float]:
        if not isinstance(v, dict):
            return {}
        return {str(k): float(val) for k, val in v.items() if isinstance(val, (int, float))}


class JobCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=20)
    application_subject_prefix: str | None = Field(default=None, max_length=120)

    @field_validator("application_subject_prefix")
    @classmethod
    def _normalize_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        if len(cleaned) < 3:
            raise ValueError("application_subject_prefix must be at least 3 characters")
        return cleaned


class JobUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, min_length=20)
    status: Literal["open", "closed"] | None = None
    application_subject_prefix: str | None = Field(default=None, max_length=120)

    @field_validator("application_subject_prefix")
    @classmethod
    def _normalize_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        if len(cleaned) < 3:
            raise ValueError("application_subject_prefix must be at least 3 characters")
        return cleaned


# ------------------------------------------------------------- candidates
class StatusUpdate(BaseModel):
    status: PipelineStatus
    reason: str | None = None


class NoteCreate(BaseModel):
    note: str = Field(min_length=1)
    next_followup_at: datetime | None = None


# ------------------------------------------------------------------- search
class CandidateFilters(BaseModel):
    skills: list[str] = Field(default_factory=list)
    location: str | None = None
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    pipeline_status: list[str] = Field(default_factory=list)
    has_github: bool | None = None
    not_contacted: bool | None = None
    query_text: str | None = None
    limit: int = 12


# -------------------------------------------------------------------- sync
class SyncSettingsIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    query: str | None = None


# --------------------------------------------------------- communications
class SendEmailRequest(BaseModel):
    candidate_id: str
    subject: str | None = None
    body: str | None = None
    kind: str = "follow_up"
    approve: bool = False


# ------------------------------------------------------------- interviews
class ScheduleRequest(BaseModel):
    candidate_id: str
    start: datetime | None = None
    duration_minutes: int = Field(default=30, ge=15, le=180)
    timezone: str | None = None
    provider: MeetingProvider | None = None
    title: str | None = None
    notes: str | None = None
    approve: bool = False
    idempotency_key: str | None = None


# ------------------------------------------------------------- workspace profile
class WorkspaceProfileIn(BaseModel):
    """Company details used automatically in every outbound email."""

    company_name: str | None = Field(default=None, max_length=200)
    company_phone: str | None = Field(default=None, max_length=60)
    interviewer_name: str | None = Field(default=None, max_length=160)


# --------------------------------------------------------------------- chat
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None
    # Optional job description attached as context for this message. The client
    # re-sends it on each message while it stays attached in the composer.
    job_id: str | None = None


# ---------------------------------------------------------------- connector
class ConnectRequest(BaseModel):
    provider: ConnectorProvider
    redirect_url: str | None = None
