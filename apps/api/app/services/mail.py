"""Mail access (Gmail).

``ComposioGmail`` talks to the real Gmail toolkit through Composio. Gmail is the
only email channel; when no account is connected :func:`get_mail_provider`
raises ``not_connected`` rather than fabricating a mailbox.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ..composio import IntegrationError, composio, normalize_tool_data
from ..config import settings
from ..http import get_client

log = logging.getLogger("recruiter.mail")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass
class AttachmentMeta:
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class MailMessage:
    provider_message_id: str
    thread_id: str | None
    sender_email: str | None
    sender_name: str | None
    recipients: list[str]
    subject: str
    snippet: str
    body_text: str
    received_at: datetime | None
    attachments: list[AttachmentMeta] = field(default_factory=list)
    headers: dict[str, Any] = field(default_factory=dict)


class MailProvider(Protocol):
    name: str

    async def list_messages(
        self, *, since: datetime | None, query: str, max_results: int = 25
    ) -> list[MailMessage]: ...

    async def download_attachment(self, message: MailMessage, attachment: AttachmentMeta) -> bytes: ...

    async def send_email(
        self, *, to: str, subject: str, body: str, is_html: bool = False
    ) -> dict[str, Any]: ...


# ------------------------------------------------------------------ helpers
def parse_sender(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = re.search(r"^(.*)<([^>]+)>", value)
    if match:
        name = match.group(1).strip().strip('"') or None
        return match.group(2).strip().lower(), name
    email = EMAIL_RE.search(value)
    return (email.group(0).lower() if email else None), None


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Gmail internalDate is milliseconds, messageTimestamp is seconds.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_ts(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.extend(EMAIL_RE.findall(item) or ([item] if "@" in item else []))
            elif isinstance(item, dict) and item.get("email"):
                out.append(str(item["email"]))
        return out
    if isinstance(value, str):
        return EMAIL_RE.findall(value)
    return []


def _normalize_message(raw: dict[str, Any]) -> MailMessage:
    message_id = (
        raw.get("messageId")
        or raw.get("message_id")
        or raw.get("id")
        or raw.get("gmail_message_id")
        or ""
    )
    sender_raw = raw.get("sender") or raw.get("from") or raw.get("From")
    sender_email, sender_name = parse_sender(sender_raw if isinstance(sender_raw, str) else None)
    if not sender_email and isinstance(sender_raw, dict):
        sender_email = (sender_raw.get("email") or "").lower() or None
        sender_name = sender_raw.get("name")

    subject = raw.get("subject")
    preview = raw.get("preview") or {}
    if not subject and isinstance(preview, dict):
        subject = preview.get("subject")
    body = raw.get("messageText") or raw.get("body") or raw.get("text")
    if not body and isinstance(preview, dict):
        body = preview.get("body")
    snippet = raw.get("snippet") or (body or "")[:280]

    attachments: list[AttachmentMeta] = []
    raw_attachments = (
        raw.get("attachmentList")
        or raw.get("attachments")
        or raw.get("payload", {}).get("parts")
        or []
    )
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename") or item.get("fileName") or item.get("name")
        if not filename:
            continue
        attachments.append(
            AttachmentMeta(
                attachment_id=str(
                    item.get("attachmentId") or item.get("attachment_id") or item.get("body", {}).get("attachmentId") or filename
                ),
                filename=str(filename),
                mime_type=str(item.get("mimeType") or item.get("mime_type") or "application/octet-stream"),
                size_bytes=int(item.get("size") or item.get("size_bytes") or 0),
            )
        )

    received = _parse_ts(
        raw.get("messageTimestamp")
        or raw.get("internalDate")
        or raw.get("date")
        or raw.get("received_at")
    )
    return MailMessage(
        provider_message_id=str(message_id),
        thread_id=str(raw.get("threadId") or raw.get("thread_id") or "") or None,
        sender_email=sender_email,
        sender_name=sender_name,
        recipients=_as_list(raw.get("to") or raw.get("recipients")),
        subject=str(subject or "").strip(),
        snippet=str(snippet or "").strip()[:280],
        body_text=str(body or ""),
        received_at=received,
        attachments=attachments,
        headers={"labels": raw.get("labelIds") or raw.get("labels") or []},
    )


def _decode_attachment_payload(data: Any) -> bytes | None:
    """Pull raw bytes out of the several shapes GMAIL_GET_ATTACHMENT can return."""
    if data is None:
        return None
    if isinstance(data, str):
        try:
            return base64.b64decode(data, validate=False)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(data, dict):
        return None

    for key in ("data", "content", "base64", "raw", "body"):
        value = data.get(key)
        if isinstance(value, str) and len(value) > 40:
            try:
                return base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
            except Exception:  # noqa: BLE001
                continue

    file_obj = data.get("file") or data.get("attachment")
    if isinstance(file_obj, dict):
        decoded = _decode_attachment_payload(file_obj)
        if decoded:
            return decoded
        for key in ("s3url", "url", "downloadUrl", "download_url"):
            url = file_obj.get(key) or data.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return ("__URL__", url)  # type: ignore[return-value]
    for key in ("s3url", "url", "downloadUrl", "download_url"):
        url = data.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return ("__URL__", url)  # type: ignore[return-value]
    return None


class ComposioGmail:
    name = "gmail"

    def __init__(self, account: dict[str, Any]) -> None:
        self.account = account
        self.connected_account_id = account.get("composio_connected_account_id")
        self.user_id = account.get("composio_user_id") or ""
        self.mailbox = account.get("connected_email")

    async def _execute(self, slug: str, arguments: dict[str, Any]) -> Any:
        return await composio.execute(
            slug,
            user_id=self.user_id,
            arguments=arguments,
            connected_account_id=self.connected_account_id,
        )

    async def list_messages(
        self, *, since: datetime | None, query: str, max_results: int = 25
    ) -> list[MailMessage]:
        effective_query = query or "newer_than:2d"
        if since is not None:
            effective_query = f"{effective_query} after:{since.strftime('%Y/%m/%d')}"
        try:
            payload = await self._execute(
                "GMAIL_FETCH_EMAILS",
                {
                    "query": effective_query,
                    "max_results": max_results,
                    "include_payload": False,
                    "user_id": "me",
                },
            )
        except IntegrationError:
            raise
        data = normalize_tool_data(payload)
        raw_messages = data.get("messages") or data.get("items") or data.get("emails") or []
        if isinstance(raw_messages, dict):
            raw_messages = [raw_messages]
        messages = [_normalize_message(m) for m in raw_messages if isinstance(m, dict)]
        return [m for m in messages if m.provider_message_id]

    async def download_attachment(self, message: MailMessage, attachment: AttachmentMeta) -> bytes:
        payload = await self._execute(
            "GMAIL_GET_ATTACHMENT",
            {
                "message_id": message.provider_message_id,
                "attachment_id": attachment.attachment_id,
                "file_name": attachment.filename,
                "user_id": "me",
            },
        )
        data = normalize_tool_data(payload)
        decoded = _decode_attachment_payload(data)
        if isinstance(decoded, tuple) and len(decoded) == 2 and decoded[0] == "__URL__":
            resp = await get_client().get(decoded[1], timeout=60)
            if resp.status_code >= 300:
                raise IntegrationError(
                    "Attachment download URL rejected", provider="gmail", code="attachment_download"
                )
            return resp.content
        if isinstance(decoded, bytes) and decoded:
            return decoded
        raise IntegrationError(
            "Gmail attachment payload did not contain file bytes",
            provider="gmail",
            code="attachment_shape",
            details={"keys": sorted(data.keys()) if isinstance(data, dict) else None},
        )

    async def send_email(self, *, to: str, subject: str, body: str, is_html: bool = False) -> dict[str, Any]:
        payload = await self._execute(
            "GMAIL_SEND_EMAIL",
            {"recipient_email": to, "subject": subject, "body": body, "is_html": is_html},
        )
        data = normalize_tool_data(payload)
        message_id = data.get("id") or data.get("messageId") or (data.get("response_data") or {}).get("id")
        return {"provider_message_id": message_id, "thread_id": data.get("threadId"), "raw_status": "sent"}


def get_mail_provider(account: dict[str, Any] | None) -> MailProvider:
    if account and account.get("composio_connected_account_id") and settings.composio_enabled:
        return ComposioGmail(account)
    raise IntegrationError(
        "No Gmail account is connected. Connect Gmail from Settings → Integrations.",
        provider="gmail",
        code="not_connected",
    )
