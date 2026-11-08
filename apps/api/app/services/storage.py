"""Resume storage (Supabase Storage only).

There is no local-disk fallback: resume bytes always live in the private
Supabase Storage bucket. The browser never receives service-role credentials;
downloads are proxied by ``GET /candidates/{id}/resume``, which streams the
object after the workspace check.
"""
from __future__ import annotations

import hashlib
import logging

from ..config import settings
from ..http import get_client

log = logging.getLogger("recruiter.storage")


class ResumeStorage:
    backend = "supabase"

    def __init__(self) -> None:
        self.bucket = settings.supabase_storage_bucket
        self.supabase_url = settings.supabase_url.rstrip("/")
        self.service_key = settings.supabase_service_role_key

    def _require_configured(self) -> None:
        if not self.supabase_url or not self.service_key:
            raise RuntimeError(
                "Supabase Storage is not configured "
                "(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required)"
            )

    def _object_url(self, path: str) -> str:
        return f"{self.supabase_url}/storage/v1/object/{self.bucket}/{path}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.service_key}",
            "apikey": self.service_key,
        }
        if extra:
            headers.update(extra)
        return headers

    async def put(self, path: str, data: bytes, content_type: str) -> str:
        self._require_configured()
        resp = await get_client().post(
            self._object_url(path),
            content=data,
            headers=self._headers(
                {"Content-Type": content_type, "x-upsert": "true", "Cache-Control": "3600"}
            ),
            timeout=60,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"Supabase Storage upload failed (HTTP {resp.status_code})")
        return path

    async def get(self, path: str) -> bytes:
        self._require_configured()
        resp = await get_client().get(self._object_url(path), headers=self._headers(), timeout=60)
        if resp.status_code >= 300:
            raise FileNotFoundError("Resume object not found in storage")
        return resp.content

    async def delete(self, path: str) -> None:
        self._require_configured()
        await get_client().delete(self._object_url(path), headers=self._headers(), timeout=30)


storage = ResumeStorage()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
