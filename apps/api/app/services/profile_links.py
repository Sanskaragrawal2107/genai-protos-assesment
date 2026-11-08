"""Read a candidate's personal website: contact links, visible content, profiles.

Resumes frequently list a personal site (e.g. ``janedoe.dev``) or a generic
"portfolio" link that itself points at the candidate's GitHub, LeetCode and
LinkedIn. Fetching the page once lets the assistant read the projects, skills
and claims it describes, and mine the anchors when canonical links are missing.

Everything here is best-effort and evidence-only: a failure returns
``available: False`` with a reason, and we never invent a link or a claim. HTML
is parsed with the standard library so no extra dependency is required.

Security note: URLs here come from untrusted resumes, so requests are restricted
to public http(s) hosts (literal loopback/private/link-local addresses are
refused). This blocks the obvious SSRF cases without a DNS round-trip.
"""
from __future__ import annotations

import ipaddress
import logging
import re
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
_MAX_TEXT_CHARS = 12_000
_MAX_HEADINGS = 40
_IGNORED_SCHEMES = {"mailto", "tel", "javascript", "data"}
# Tags whose text is code/markup noise rather than page content.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
# Tags that should start/end a line in the extracted text.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "nav",
    "ul", "ol", "li", "table", "tr", "br",
    "h1", "h2", "h3", "h4", "h5", "h6",
}

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


def normalize_url(url: str | None) -> str | None:
    """Return an absolute URL, upgrading a schemeless host to https."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        return url
    return f"https://{url}"


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


class _PageParser(HTMLParser):
    """Collect anchors, title/description, headings and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.title_parts: list[str] = []
        self.description: str = ""
        self.headings: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.hrefs.append(value)
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3", "h4"):
            self._heading_tag = tag
            self._heading_parts = []
        elif tag == "meta":
            data = {k.lower(): (v or "") for k, v in attrs}
            name = (data.get("name") or data.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = data.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            text = " ".join("".join(self._heading_parts).split())
            if text and len(self.headings) < _MAX_HEADINGS:
                self.headings.append({"tag": tag, "text": text[:160]})
            self._heading_tag = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._heading_tag:
            self._heading_parts.append(data)
        self.text_parts.append(data)


def _clean_text(parts: list[str]) -> str:
    """Collapse raw text nodes into readable, bounded lines."""
    text = "".join(parts)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:_MAX_TEXT_CHARS]


def parse_website(html: str, base_url: str) -> dict[str, Any]:
    """Return the page's identity, visible text, headings and links.

    Links are resolved against ``base_url``, de-duplicated in document order and
    grouped by :func:`classify_url`. ``text`` is the readable page content (with
    scripts/styles removed) so the assistant can summarise projects and claims
    even when a site has no machine-readable data.
    """
    parser = _PageParser()
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
    return {
        "title": title,
        "description": description,
        "headings": parser.headings,
        "text": _clean_text(parser.text_parts),
        "links": buckets,
    }


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
    url = normalize_url(url) or url
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
            "headings": parsed["headings"],
            "text": parsed["text"],
            "links": parsed["links"],
            "caveat": (
                "Content and links read from the candidate's own website. Presence is not "
                "proof of ownership or quality; profile links are verified separately when possible."
            ),
        }
    )
    return base
