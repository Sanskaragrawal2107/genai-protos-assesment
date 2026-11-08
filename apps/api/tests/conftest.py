import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

API_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(TESTS_DIR))

# Tests run against a real Postgres database (the same engine as production).
# Point TEST_DATABASE_URL at a scratch database — the suite drops and recreates
# every table, so never use the Supabase project database here.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL or "postgresql+asyncpg://invalid:invalid@db.invalid:5432/placeholder",
        "OPENAI_API_KEY": "",
        "COMPOSIO_API_KEY": "",
        "GITHUB_TOKEN": "",
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
        "SUPABASE_JWT_SECRET": "test-jwt-secret",
        "SUPABASE_STORAGE_BUCKET": "resumes",
        "API_INTERNAL_TOKEN": "test-internal-token-0123456789",
        "WEB_ORIGIN": "https://app.test",
    }
)


def pytest_collection_modifyitems(config, items):
    if not TEST_DATABASE_URL:
        skip = pytest.mark.skip(
            reason="TEST_DATABASE_URL is not set — requires a Postgres test database."
        )
        for item in items:
            if "db" in getattr(item, "fixturenames", []) or "workspace" in getattr(item, "fixturenames", []):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def in_memory_storage(monkeypatch):
    """Route resume storage to memory so no Supabase calls happen in tests."""
    from app.services import storage as storage_module
    from support import InMemoryStorage

    fake = InMemoryStorage()
    monkeypatch.setattr(storage_module.storage, "put", fake.put)
    monkeypatch.setattr(storage_module.storage, "get", fake.get)
    monkeypatch.setattr(storage_module.storage, "delete", fake.delete)
    return fake


@pytest_asyncio.fixture
async def db():
    from app import db as db_module

    await db_module.reset_engine()
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.metadata.drop_all)
        await conn.run_sync(db_module.metadata.create_all)
    async with db_module.session() as session:
        yield session
    await db_module.reset_engine()


@pytest_asyncio.fixture
async def workspace(db):
    from app import db as db_module
    from app.db import connected_accounts, inbox_sync_settings, utcnow

    import sqlalchemy as sa

    ws_id = db_module.new_id()
    now = utcnow()
    await db.execute(
        sa.insert(db_module.workspaces).values(
            id=ws_id, name="Test workspace", settings={"meeting_provider": "microsoft_teams"}, created_at=now, updated_at=now
        )
    )
    await db.execute(
        sa.insert(db_module.workspace_members).values(
            id=db_module.new_id(),
            workspace_id=ws_id,
            user_id="tester",
            email="tester@example.com",
            role="owner",
            created_at=now,
        )
    )
    account_id = db_module.new_id()
    await db.execute(
        sa.insert(connected_accounts).values(
            id=account_id,
            workspace_id=ws_id,
            recruiter_id="tester",
            provider="gmail",
            connected_email="recruiting@example.com",
            status="active",
            scopes=[],
            created_at=now,
            updated_at=now,
        )
    )
    await db.execute(
        sa.insert(inbox_sync_settings).values(
            id=db_module.new_id(),
            workspace_id=ws_id,
            connected_account_id=account_id,
            enabled=True,
            interval_minutes=30,
            is_custom_interval=False,
            query="newer_than:2d",
            next_sync_at=now,
            consecutive_failures=0,
            overlap_minutes=60,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    yield {"workspace_id": ws_id, "account_id": account_id}
