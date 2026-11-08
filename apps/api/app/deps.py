"""Shared FastAPI dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session
from .repo import ensure_workspace
from .security import Identity, get_identity


@dataclass(slots=True)
class Ctx:
    session: AsyncSession
    identity: Identity
    workspace_id: str

    @property
    def user_id(self) -> str:
        return self.identity.user_id


async def ctx_dependency(identity: Identity = Depends(get_identity)) -> AsyncGenerator[Ctx, None]:
    async with session() as db:
        workspace_id = await ensure_workspace(db, identity)
        yield Ctx(session=db, identity=identity, workspace_id=workspace_id)


CtxDep = Annotated[Ctx, Depends(ctx_dependency)]
