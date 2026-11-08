"""Discover a candidate's public profiles from a personal website.

Resumes frequently list a personal site (e.g. ``janedoe.dev``) or a generic
"portfolio" link that itself points at the candidate's GitHub, LeetCode and
LinkedIn. When those canonical URLs are not stated on the resume, we fetch the
site once and mine its anchors so verification has something real to check.

Everything here is best-effort and evidence-only: a failure returns
``available: False`` with a reason, and we never invent a link. HTML is parsed
with the standard library so no extra dependency is required.

Security note: URLs here come from untrusted resumes, so requests are restricted
to public http(s) hosts (literal loopback/private/link-local addresses are
refused). This blocks the obvious SSRF cases without a DNS round-trip.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..http import get_client

log = logging.getLogger("recruiter.profile_links")

# Bounds that keep one hostile page from exhausting memory or a worker.
_MAX_HTML_BYTES = 600_000
_MAX_LINKS_PER_KIND = 5
_IGNORED_SCHEMES = {"mailto", "tel", "javascript", "data"}

# Hosts whose subdomains map to a canonical profile field.
_SOCIAL_HOSTS: dict[str, tuple[str, ...]] = {
    "github": ("github.com",),
    "leetcode": ("leetcode.com",),
    "linkedin": ("linkedin.com",),
}

_USER_AGENT = "Mozilla/5.0 (compatible; ai-recruiter-os)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_of(url: str) -> str | None:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    return (parsed.hostname or "").lower() or None


def classify_url(url: str | None) -> str:
    """Map a URL to ``github`` / ``leetcode`` / ``linkedin`` / ``website``.

    A URL with no recognisable social host is treated as a personal ``website``,
    which is what a portfolio link should be called.
    """
    if not url:
        return "website"
    host = _host_of(url)
    if not host:
        return "website"
    for kind, hosts in _SOCIAL_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return kind
    return "website"


def is_public_url(url: str | None) -> bool:
    """Whether a URL is safe to fetch: http(s) to a non-private host.

    The scheme is read from the raw value first. That way a schemeless host
    (``janedoe.dev``) is upgraded to https, while a non-web scheme
    (``javascript:``, ``mailto:``, ``ftp:``) is rejected instead of being
    mistaken for a hostname.
    """
    if not url:
        return False
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme:
        if parsed.scheme.lower() not in ("http", "https"):
            return False
    else:
        try:
            parsed = urlparse(f"https://{url}")
        except ValueError:
            return False
    host = parsed.hostname
    if not host:
        return False
    host = host.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname; DNS is resolved by httpx
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


class _AnchorParser(HTMLParser):
    """Collect anchor hrefs plus the page title/description."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.title_parts: list[str] = []
        self.description: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.hrefs.append(value)
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            data = {k.lower(): (v or "") for k, v in attrs}
            name = (data.get("name") or data.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = data.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def parse_website(html: str, base_url: str) -> dict[str, Any]:
    """Return ``{title, description, links: {kind: [url, ...]}}`` for a page.

    Links are resolved against ``base_url``, de-duplicated in document order and
    grouped by :func:`classify_url`. Social links are ordered ahead of generic
    ones so a verification pass sees GitHub/LeetCode first.
    """
    parser = _AnchorParser()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001 - malformed markup from the wild
        log.info("website HTML parse issue (%s)", exc.__class__.__name__)

    buckets: dict[str, list[str]] = {"github": [], "leetcode": [], "linkedin": [], "website": []}
    seen: set[str] = set()
    for href in parser.hrefs:
        href = href.strip()
        if not href or href.startswith("#"):
            continue
        scheme = href.split(":", 1)[0].lower()
        if scheme in _IGNORED_SCHEMES:
            continue
        absolute = urljoin(base_url, href)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if not is_public_url(absolute):
            continue
        key = absolute.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        kind = classify_url(absolute)
        if len(buckets[kind]) < _MAX_LINKS_PER_KIND:
            buckets[kind].append(absolute)

    title = " ".join("".join(parser.title_parts).split())[:200] or None
    description = " ".join(parser.description.split())[:400] or None
    return {"title": title, "description": description, "links": buckets}


def profiles_from_links(links: dict[str, list[str]]) -> dict[str, str]:
    """Pick the first discovered URL per canonical profile field."""
    found: dict[str, str] = {}
    for kind, field in (("github", "github_url"), ("leetcode", "leetcode_url"), ("linkedin", "linkedin_url")):
        values = links.get(kind) or []
        if values:
            found[field] = values[0]
    return found


async def fetch_website(url: str | None) -> dict[str, Any]:
    """Fetch a personal website and return its identity plus discovered links."""
    base: dict[str, Any] = {
        "provider": "website",
        "source_url": url,
        "available": False,
        "reason": None,
        "fetched_at": _now(),
    }
    if not url:
        base["reason"] = "No personal website or portfolio URL on the resume"
        return base
    if not is_public_url(url):
        base["reason"] = "Personal website URL is missing or not a public http(s) address"
        return base

    try:
        resp = await get_client().get(
            url,
            timeout=20,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
    except httpx.HTTPError as exc:
        base["reason"] = f"Website request failed ({exc.__class__.__name__})"
        return base

    if resp.status_code >= 400:
        base["reason"] = f"Website returned HTTP {resp.status_code}"
        return base
    content_type = (resp.headers.get("content-type") or "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        base["reason"] = f"Website is not an HTML page ({content_type.split(';')[0].strip()})"
        return base

    parsed = parse_website(resp.text[:_MAX_HTML_BYTES], str(resp.url))
    base.update(
        {
            "available": True,
            "reason": None,
            "final_url": str(resp.url),
            "title": parsed["title"],
            "description": parsed["description"],
            "links": parsed["links"],
            "caveat": (
                "Links found on the candidate's own website. Their presence is not proof of "
                "ownership or quality; each is verified separately when possible."
            ),
        }
    )
    return base
