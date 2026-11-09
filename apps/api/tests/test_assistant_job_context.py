"""Assistant JD context: prompt rendering and workspace-scoped job lookup."""
from __future__ import annotations

import sqlalchemy as sa

from app import db as db_module
from app.db import jobs, utcnow
from app.graphs.assistant_graph import _job_context_block, _load_job


def test_job_context_block_renders_the_jd():
    job = {
        "id": "job-1",
        "title": "Senior AI Engineer",
        "status": "open",
        "description": "Build RAG pipelines with Python and FastAPI.",
        "structured_requirements": {
            "required_skills": ["Python", "FastAPI"],
            "min_experience_years": 4,
        },
        "application_subject_prefix": "Application:",
    }

    block = _job_context_block(job)

    assert "ATTACHED JOB DESCRIPTION" in block
    assert "Senior AI Engineer" in block
    assert "Build RAG pipelines with Python and FastAPI." in block
    assert "Python" in block
    assert "target role" in block


async def test_load_job_is_workspace_scoped(db, workspace):
    now = utcnow()
    job_id = db_module.new_id()
    await db.execute(
        sa.insert(jobs).values(
            id=job_id,
            workspace_id=workspace["workspace_id"],
            title="ML Engineer",
            description="Train and ship models.",
            structured_requirements={},
            status="open",
            created_by="tester",
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    loaded = await _load_job(db, workspace["workspace_id"], job_id)
    assert loaded is not None
    assert loaded["title"] == "ML Engineer"

    # A job from another workspace must never be loadable.
    assert await _load_job(db, db_module.new_id(), job_id) is None
    assert await _load_job(db, workspace["workspace_id"], None) is None
