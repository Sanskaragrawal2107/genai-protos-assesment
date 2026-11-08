"""Database schema + engine (Supabase Postgres only).

The schema is expressed twice on purpose:

* this module (SQLAlchemy Core) — the tables the API reads and writes, and the
  ones the tests create against a Postgres test database;
* ``supabase/migrations/0001_schema.sql`` — the authoritative deployment schema
  (timestamptz/jsonb/RLS/pg_cron). Both must be kept in sync.

There is no SQLite/local mode: the engine is always an async Postgres
(asyncpg) connection, and the deployed schema is owned by the SQL migrations
rather than created at boot.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(sa.TypeDecorator):
    """tz-aware UTC datetime stored as timestamptz."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


JSONType = sa.JSON().with_variant(postgresql.JSONB, "postgresql")
TS = UTCDateTime()


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, TS, nullable=nullable)


metadata = sa.MetaData()

# --------------------------------------------------------------------- tenancy
workspaces = sa.Table(
    "workspaces",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("settings", JSONType, nullable=False, default=dict),
    _ts("created_at"),
    _ts("updated_at"),
)

workspace_members = sa.Table(
    "workspace_members",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("email", sa.Text),
    sa.Column("display_name", sa.Text),
    sa.Column("role", sa.String(20), nullable=False, default="owner"),
    _ts("created_at"),
    sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
)
sa.Index("ix_workspace_members_user", workspace_members.c.user_id)

# ------------------------------------------------------------------ connectors
connected_accounts = sa.Table(
    "connected_accounts",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text, nullable=False),
    sa.Column("provider", sa.String(40), nullable=False),  # gmail | microsoft_teams
    sa.Column("composio_connected_account_id", sa.Text),
    sa.Column("composio_auth_config_id", sa.Text),
    sa.Column("composio_user_id", sa.Text),
    sa.Column("connected_email", sa.Text),
    sa.Column("display_name", sa.Text),
    sa.Column("status", sa.String(20), nullable=False, default="pending"),
    sa.Column("scopes", JSONType, nullable=False, default=list),
    sa.Column("last_error", sa.Text),
    _ts("last_verified_at", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
    sa.UniqueConstraint("workspace_id", "provider", "connected_email", name="uq_connected_account"),
)
sa.Index("ix_connected_accounts_ws", connected_accounts.c.workspace_id, connected_accounts.c.provider)

inbox_sync_settings = sa.Table(
    "inbox_sync_settings",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column(
        "connected_account_id",
        sa.String(36),
        sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("enabled", sa.Boolean, nullable=False, default=True),
    sa.Column("interval_minutes", sa.Integer, nullable=False, default=30),
    sa.Column("is_custom_interval", sa.Boolean, nullable=False, default=False),
    sa.Column("query", sa.Text, nullable=False, default="newer_than:2d"),
    _ts("last_synced_at", nullable=True),
    _ts("next_sync_at", nullable=True),
    sa.Column("last_sync_status", sa.String(20)),
    sa.Column("last_sync_error", sa.Text),
    sa.Column("consecutive_failures", sa.Integer, nullable=False, default=0),
    sa.Column("history_cursor", sa.Text),
    sa.Column("overlap_minutes", sa.Integer, nullable=False, default=60),
    _ts("created_at"),
    _ts("updated_at"),
    sa.UniqueConstraint("connected_account_id", name="uq_sync_settings_account"),
)
sa.Index("ix_sync_settings_due", inbox_sync_settings.c.enabled, inbox_sync_settings.c.next_sync_at)

ingestion_jobs = sa.Table(
    "ingestion_jobs",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column(
        "connected_account_id",
        sa.String(36),
        sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("status", sa.String(20), nullable=False, default="queued"),
    sa.Column("trigger", sa.String(20), nullable=False, default="cron"),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("max_attempts", sa.Integer, nullable=False, default=4),
    sa.Column("locked_by", sa.Text),
    _ts("locked_at", nullable=True),
    _ts("started_at", nullable=True),
    _ts("completed_at", nullable=True),
    _ts("next_retry_at", nullable=True),
    sa.Column("error_message", sa.Text),
    sa.Column("stats", JSONType, nullable=False, default=dict),
    _ts("created_at"),
)
sa.Index("ix_jobs_ws_created", ingestion_jobs.c.workspace_id, ingestion_jobs.c.created_at)
sa.Index("ix_jobs_status_retry", ingestion_jobs.c.status, ingestion_jobs.c.next_retry_at)
sa.Index("ix_jobs_account_status", ingestion_jobs.c.connected_account_id, ingestion_jobs.c.status)

email_messages = sa.Table(
    "email_messages",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column(
        "connected_account_id",
        sa.String(36),
        sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("provider_message_id", sa.Text, nullable=False),
    sa.Column("thread_id", sa.Text),
    sa.Column("sender_email", sa.Text),
    sa.Column("sender_name", sa.Text),
    sa.Column("recipients", JSONType, nullable=False, default=list),
    sa.Column("subject", sa.Text),
    sa.Column("snippet", sa.Text),
    _ts("received_at", nullable=True),
    sa.Column("has_attachments", sa.Boolean, nullable=False, default=False),
    sa.Column("processing_status", sa.String(20), nullable=False, default="pending"),
    sa.Column("skip_reason", sa.Text),
    sa.Column("error_message", sa.Text),
    sa.Column("headers", JSONType, nullable=False, default=dict),
    _ts("created_at"),
    sa.UniqueConstraint("connected_account_id", "provider_message_id", name="uq_email_provider_id"),
)
sa.Index("ix_email_messages_ws", email_messages.c.workspace_id, email_messages.c.received_at)

email_attachments = sa.Table(
    "email_attachments",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column(
        "email_message_id",
        sa.String(36),
        sa.ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("provider_attachment_id", sa.Text, nullable=False),
    sa.Column("filename", sa.Text),
    sa.Column("mime_type", sa.Text),
    sa.Column("size_bytes", sa.Integer, nullable=False, default=0),
    sa.Column("storage_path", sa.Text),
    sa.Column("sha256", sa.String(64)),
    sa.Column("status", sa.String(20), nullable=False, default="pending"),
    sa.Column("error_message", sa.Text),
    _ts("created_at"),
    sa.UniqueConstraint("email_message_id", "provider_attachment_id", name="uq_attachment_provider_id"),
)
sa.Index("ix_attachments_message", email_attachments.c.email_message_id)

# ------------------------------------------------------------------- hires
candidates = sa.Table(
    "candidates",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("full_name", sa.Text, nullable=False),
    sa.Column("email", sa.Text),
    sa.Column("email_normalized", sa.Text),
    sa.Column("phone", sa.Text),
    sa.Column("location", sa.Text),
    sa.Column("preferred_location", sa.Text),
    sa.Column("experience_years", sa.Float),
    sa.Column("current_role", sa.Text),
    sa.Column("current_company", sa.Text),
    sa.Column("previous_companies", JSONType, nullable=False, default=list),
    sa.Column("education", JSONType, nullable=False, default=list),
    sa.Column("university", sa.Text),
    sa.Column("graduation_year", sa.Integer),
    sa.Column("skills", JSONType, nullable=False, default=list),
    sa.Column("programming_languages", JSONType, nullable=False, default=list),
    sa.Column("frameworks", JSONType, nullable=False, default=list),
    sa.Column("tools", JSONType, nullable=False, default=list),
    sa.Column("projects", JSONType, nullable=False, default=list),
    sa.Column("summary", sa.Text),
    sa.Column("github_url", sa.Text),
    sa.Column("leetcode_url", sa.Text),
    sa.Column("linkedin_url", sa.Text),
    sa.Column("portfolio_url", sa.Text),
    sa.Column("resume_storage_path", sa.Text),
    sa.Column("resume_filename", sa.Text),
    sa.Column("resume_text", sa.Text),
    sa.Column("resume_sha256", sa.String(64)),
    sa.Column("extraction_method", sa.String(20)),
    sa.Column("extraction_confidence", sa.Float),
    sa.Column("extraction", JSONType, nullable=False, default=dict),
    sa.Column("pipeline_status", sa.String(30), nullable=False, default="APPLIED"),
    sa.Column("source", sa.String(30), nullable=False, default="gmail"),
    sa.Column("owner_id", sa.Text),
    _ts("created_at"),
    _ts("updated_at"),
    sa.UniqueConstraint("workspace_id", "email_normalized", name="uq_candidate_email"),
)
sa.Index("ix_candidates_ws_status", candidates.c.workspace_id, candidates.c.pipeline_status)
sa.Index("ix_candidates_ws_created", candidates.c.workspace_id, candidates.c.created_at)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("structured_requirements", JSONType, nullable=False, default=dict),
    sa.Column("status", sa.String(20), nullable=False, default="open"),
    sa.Column("application_subject_prefix", sa.Text),
    sa.Column("created_by", sa.Text),
    _ts("created_at"),
    _ts("updated_at"),
)
sa.Index("ix_jobs_ws", jobs.c.workspace_id, jobs.c.status)

candidate_applications = sa.Table(
    "candidate_applications",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("email_message_id", sa.String(36), sa.ForeignKey("email_messages.id", ondelete="SET NULL")),
    sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
    _ts("application_date", nullable=True),
    _ts("created_at"),
    sa.UniqueConstraint("candidate_id", "email_message_id", name="uq_application_message"),
)
sa.Index("ix_applications_candidate", candidate_applications.c.candidate_id)

candidate_evaluations = sa.Table(
    "candidate_evaluations",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    sa.Column("match_score", sa.Float, nullable=False, default=0),
    sa.Column("skills_score", sa.Float, nullable=False, default=0),
    sa.Column("experience_score", sa.Float, nullable=False, default=0),
    sa.Column("project_score", sa.Float, nullable=False, default=0),
    sa.Column("location_score", sa.Float, nullable=False, default=0),
    sa.Column("evidence_score", sa.Float, nullable=False, default=0),
    sa.Column("weights", JSONType, nullable=False, default=dict),
    sa.Column("evaluation_details", JSONType, nullable=False, default=dict),
    sa.Column("strengths", JSONType, nullable=False, default=list),
    sa.Column("missing_requirements", JSONType, nullable=False, default=list),
    sa.Column("concerns", JSONType, nullable=False, default=list),
    sa.Column("verification_evidence", JSONType, nullable=False, default=dict),
    sa.Column("explanation", sa.Text),
    sa.Column("model", sa.Text),
    sa.Column("method", sa.String(20), nullable=False, default="heuristic"),
    sa.Column("input_hash", sa.String(64)),
    _ts("evaluated_at"),
    sa.UniqueConstraint("candidate_id", "job_id", name="uq_evaluation_candidate_job"),
)
sa.Index("ix_evaluations_job_score", candidate_evaluations.c.job_id, candidate_evaluations.c.match_score)

candidate_communications = sa.Table(
    "candidate_communications",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text),
    sa.Column("channel", sa.String(20), nullable=False, default="email"),
    sa.Column("direction", sa.String(10), nullable=False, default="outbound"),
    sa.Column("kind", sa.String(30), nullable=False, default="custom"),
    sa.Column("subject", sa.Text),
    sa.Column("content", sa.Text),
    sa.Column("provider", sa.String(30), nullable=False, default="gmail"),
    sa.Column("provider_message_id", sa.Text),
    sa.Column("status", sa.String(20), nullable=False, default="draft"),
    sa.Column("error_message", sa.Text),
    sa.Column("metadata", JSONType, nullable=False, default=dict),
    _ts("created_at"),
    _ts("sent_at", nullable=True),
)
sa.Index("ix_comms_candidate", candidate_communications.c.candidate_id, candidate_communications.c.created_at)
sa.Index(
    "ix_comms_candidate_provider",
    candidate_communications.c.candidate_id,
    candidate_communications.c.provider_message_id,
)

interviews = sa.Table(
    "interviews",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text),
    sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
    sa.Column("provider", sa.String(30), nullable=False, default="microsoft_teams"),
    sa.Column("calendar_provider", sa.String(30), nullable=False, default="teams"),
    sa.Column("calendar_id", sa.Text, nullable=False, default="primary"),
    sa.Column("calendar_event_id", sa.Text),
    sa.Column("calendar_html_link", sa.Text),
    sa.Column("meeting_id", sa.Text),
    sa.Column("meeting_url", sa.Text),
    _ts("start_time", nullable=True),
    _ts("end_time", nullable=True),
    sa.Column("timezone", sa.Text, nullable=False, default="Asia/Kolkata"),
    sa.Column("status", sa.String(30), nullable=False, default="scheduled"),
    sa.Column("invite_status", sa.String(20), nullable=False, default="not_sent"),
    sa.Column("attendees", JSONType, nullable=False, default=list),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("notes", sa.Text),
    _ts("created_at"),
    _ts("updated_at"),
    sa.UniqueConstraint("idempotency_key", name="uq_interview_idempotency"),
)
sa.Index("ix_interviews_ws_start", interviews.c.workspace_id, interviews.c.start_time)
sa.Index("ix_interviews_candidate", interviews.c.candidate_id)

candidate_status_history = sa.Table(
    "candidate_status_history",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("from_status", sa.String(30)),
    sa.Column("to_status", sa.String(30), nullable=False),
    sa.Column("changed_by", sa.Text),
    sa.Column("reason", sa.Text),
    _ts("created_at"),
)
sa.Index("ix_status_history_candidate", candidate_status_history.c.candidate_id)

recruiter_notes = sa.Table(
    "recruiter_notes",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("candidate_id", sa.String(36), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text),
    sa.Column("note", sa.Text, nullable=False),
    _ts("next_followup_at", nullable=True),
    _ts("created_at"),
)
sa.Index("ix_notes_candidate", recruiter_notes.c.candidate_id)

recruiter_activity_logs = sa.Table(
    "recruiter_activity_logs",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("entity_type", sa.Text),
    sa.Column("entity_id", sa.Text),
    sa.Column("metadata", JSONType, nullable=False, default=dict),
    _ts("created_at"),
)
sa.Index("ix_activity_ws_created", recruiter_activity_logs.c.workspace_id, recruiter_activity_logs.c.created_at)

pending_actions = sa.Table(
    "pending_actions",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("recruiter_id", sa.Text),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("payload", JSONType, nullable=False, default=dict),
    sa.Column("status", sa.String(20), nullable=False, default="pending"),
    sa.Column("result", JSONType, nullable=False, default=dict),
    sa.Column("error_message", sa.Text),
    _ts("created_at"),
    _ts("expires_at", nullable=True),
    _ts("executed_at", nullable=True),
)
sa.Index("ix_pending_actions_ws", pending_actions.c.workspace_id, pending_actions.c.status)

chat_threads = sa.Table(
    "chat_threads",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("user_id", sa.Text),
    sa.Column("title", sa.Text, nullable=False, default="New conversation"),
    _ts("created_at"),
    _ts("updated_at"),
)
sa.Index("ix_chat_threads_ws", chat_threads.c.workspace_id, chat_threads.c.updated_at)

chat_messages = sa.Table(
    "chat_messages",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True, default=new_id),
    sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False),
    sa.Column("role", sa.String(20), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("payload", JSONType, nullable=False, default=dict),
    _ts("created_at"),
)
sa.Index("ix_chat_messages_thread", chat_messages.c.thread_id, chat_messages.c.created_at)

ALL_TABLES = list(metadata.sorted_tables)

# --------------------------------------------------------------------- engine
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    url = settings.database_url
    return create_async_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=False,
        pool_recycle=300,
        pool_size=10,
        max_overflow=10,
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db() -> None:
    """Verify database connectivity.

    The schema (tables, RLS, pg_cron) is owned by ``supabase/migrations``; we
    never create tables at boot. This only fails fast if Postgres is
    unreachable so a broken deployment is obvious immediately.
    """
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(sa.text("SELECT 1"))


async def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests and scripts)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def session() -> AsyncSession:
    return get_sessionmaker()()
