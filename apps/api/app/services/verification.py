"""Candidate claim verification against public sources.

GitHub: public REST API (no key required, optional token raises the rate limit).
LeetCode: the public GraphQL endpoint used by leetcode.com itself. It is not a
documented, supported API, so a failure is reported as ``available: false`` with
a reason — never as zero activity.

Every payload returned here carries ``source_url`` and ``fetched_at`` so the UI
can show where a claim was checked. Absence of evidence is stated explicitly and
is never converted into a negative claim about the candidate.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..http import get_client
from . import profile_links

log = logging.getLogger("recruiter.verify")

GITHUB_API = "https://api.github.com"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"

_LEETCODE_QUERY = """
query userProfile($username: String!) {
  matchedUser(username: $username) {
    username
    profile { ranking reputation }
    submitStats {
      acSubmissionNum { difficulty count submissions }
      totalSubmissionNum { difficulty count }
    }
    badges { displayName }
  }
}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_username(url: str | None, host: str) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    if host not in (parsed.netloc or "").lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if host == "leetcode.com" and parts and parts[0] in {"u", "profile"}:
        parts = parts[1:]
    return parts[0] if parts else None


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+#.]+", text.lower()) if len(t) > 2}


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-recruiter-os",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


async def verify_github(url: str | None, *, keywords: list[str] | None = None) -> dict[str, Any]:
    username = parse_username(url, "github.com")
    base: dict[str, Any] = {
        "provider": "github",
        "source_url": url,
        "username": username,
        "available": False,
        "reason": None,
        "fetched_at": _now(),
    }
    if not username:
        base["reason"] = "No GitHub profile URL on the resume"
        return base

    keywords = [k for k in (keywords or []) if k]
    try:
        client = get_client()
        profile_resp, repos_resp = await asyncio.gather(
            client.get(f"{GITHUB_API}/users/{username}", timeout=25, headers=_headers()),
            client.get(
                f"{GITHUB_API}/users/{username}/repos",
                params={"sort": "pushed", "per_page": 30, "type": "owner"},
                timeout=25,
                headers=_headers(),
            ),
        )
        if profile_resp.status_code == 404:
            base["reason"] = "GitHub profile not found (404)"
            return base
        if profile_resp.status_code in (403, 429):
            base["reason"] = "GitHub API rate limit reached; try again later or set GITHUB_TOKEN"
            return base
        if profile_resp.status_code >= 400:
            base["reason"] = f"GitHub API returned HTTP {profile_resp.status_code}"
            return base
        profile = profile_resp.json()
        repos: list[dict[str, Any]] = repos_resp.json() if repos_resp.status_code == 200 else []
    except httpx.HTTPError as exc:
        base["reason"] = f"GitHub request failed ({exc.__class__.__name__})"
        return base

    keyword_tokens: set[str] = set()
    for keyword in keywords:
        keyword_tokens |= _tokens(keyword)

    relevant: list[dict[str, Any]] = []
    languages: dict[str, int] = {}
    for repo in repos:
        if repo.get("fork"):
            continue
        language = repo.get("language")
        if language:
            languages[language] = languages.get(language, 0) + 1
        haystack = " ".join(
            [
                repo.get("name") or "",
                repo.get("description") or "",
                " ".join(repo.get("topics") or []),
                language or "",
            ]
        )
        hits = sorted(keyword_tokens & _tokens(haystack))
        if hits:
            relevant.append(
                {
                    "name": repo.get("name"),
                    "url": repo.get("html_url"),
                    "description": repo.get("description"),
                    "language": language,
                    "stars": repo.get("stargazers_count", 0),
                    "pushed_at": repo.get("pushed_at"),
                    "matched_keywords": hits,
                }
            )
        if len(relevant) >= 8:
            break

    active = [r for r in repos if not r.get("fork")]
    last_push = max((r.get("pushed_at") or "" for r in active), default="") or None
    base.update(
        {
            "available": True,
            "reason": None,
            "profile_url": profile.get("html_url"),
            "public_repos": profile.get("public_repos"),
            "followers": profile.get("followers"),
            "account_created": profile.get("created_at"),
            "last_push_at": last_push,
            "top_languages": sorted(languages.items(), key=lambda kv: kv[1], reverse=True)[:6],
            "repositories_considered": len(active),
            "relevant_repositories": relevant,
            "verified_keywords": sorted({k for r in relevant for k in r["matched_keywords"]}),
            "caveat": (
                "Repository metadata is public evidence that projects exist and mention these "
                "technologies. It is not proof of authorship quality or production experience."
            ),
        }
    )
    return base


async def verify_leetcode(url: str | None) -> dict[str, Any]:
    username = parse_username(url, "leetcode.com")
    base: dict[str, Any] = {
        "provider": "leetcode",
        "source_url": url,
        "username": username,
        "available": False,
        "reason": None,
        "fetched_at": _now(),
    }
    if not username:
        base["reason"] = "No LeetCode profile URL on the resume"
        return base

    # The public GraphQL endpoint is primary: it is the one that actually
    # answers today and returns in about a second. The hosted MCP server (if
    # configured) is only consulted as an opt-in last resort when GraphQL is
    # unavailable, so a broken MCP handshake never adds latency to the normal
    # path. ``LEETCODE_MCP_FALLBACK=true`` turns that last resort on.
    graphql = await _verify_leetcode_graphql(username)
    if graphql.get("available") or not (settings.leetcode_mcp_enabled and settings.leetcode_mcp_fallback):
        return graphql

    from .leetcode_mcp import fetch_leetcode_evidence

    evidence = await fetch_leetcode_evidence(username)
    if evidence.get("available"):
        evidence["fallback_from"] = graphql.get("source")
        evidence["fallback_reason"] = graphql.get("reason")
        return evidence
    graphql["fallback_reason"] = evidence.get("reason")
    return graphql


async def _verify_leetcode_graphql(username: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "leetcode",
        "source": "leetcode.com public GraphQL (unofficial endpoint)",
        "source_url": f"https://leetcode.com/{username}/",
        "username": username,
        "available": False,
        "reason": None,
        "fetched_at": _now(),
    }
    payload = {
        "query": _LEETCODE_QUERY,
        "variables": {"username": username},
        "operationName": "userProfile",
    }
    headers = {
        "Content-Type": "application/json",
        "Referer": f"https://leetcode.com/{username}/",
        "User-Agent": "Mozilla/5.0 (compatible; ai-recruiter-os)",
    }
    try:
        resp = await get_client().post(LEETCODE_GRAPHQL, json=payload, headers=headers, timeout=25)
    except httpx.HTTPError as exc:
        base["reason"] = f"LeetCode request failed ({exc.__class__.__name__})"
        return base

    if resp.status_code >= 400:
        base["reason"] = f"LeetCode returned HTTP {resp.status_code} (public endpoint unavailable)"
        return base
    try:
        body = resp.json()
    except ValueError:
        base["reason"] = "LeetCode returned a non-JSON response (likely bot protection)"
        return base
    matched = ((body.get("data") or {}).get("matchedUser")) or None
    if not matched:
        base["reason"] = "LeetCode profile not found or hidden"
        return base

    stats = matched.get("submitStats") or {}
    solved_by_difficulty = {
        entry.get("difficulty"): entry.get("count")
        for entry in stats.get("acSubmissionNum") or []
        if isinstance(entry, dict)
    }
    total_solved = solved_by_difficulty.get("All")
    base.update(
        {
            "available": True,
            "profile_url": f"https://leetcode.com/{username}/",
            "ranking": (matched.get("profile") or {}).get("ranking"),
            "reputation": (matched.get("profile") or {}).get("reputation"),
            "total_solved": total_solved,
            "solved_by_difficulty": {
                key: value for key, value in solved_by_difficulty.items() if key and key != "All"
            },
            "caveat": "Problem counts show practice volume, not verified engineering ability.",
        }
    )
    return base


def evidence_score(github: dict[str, Any], leetcode: dict[str, Any]) -> tuple[float, list[str]]:
    """0-100 evidence score from *available* public signals only."""
    signals: list[str] = []
    score = 0.0
    if github.get("available"):
        relevant = len(github.get("relevant_repositories") or [])
        repos = github.get("repositories_considered") or 0
        if relevant:
            score += min(55.0, 18.0 * relevant)
            signals.append(
                f"{relevant} public repo(s) mention job keywords: "
                + ", ".join(github.get("verified_keywords") or [])[:160]
            )
        elif repos:
            score += 12.0
            signals.append(f"{repos} public repo(s) found but none matched the job keywords")
        if github.get("last_push_at"):
            signals.append(f"Most recent public push: {github['last_push_at'][:10]}")
    else:
        signals.append(f"GitHub not verified: {github.get('reason')}")

    if leetcode.get("available"):
        solved = leetcode.get("total_solved") or 0
        rating = leetcode.get("contest_rating")
        if solved:
            score += min(35.0, solved / 6.0)
            signals.append(f"LeetCode: {solved} problems solved")
        elif rating:
            score += min(35.0, max(0.0, (float(rating) - 1200.0) / 12.0 + 12.0))
        if rating:
            signals.append(f"LeetCode contest rating: {float(rating):.0f}")
        if not solved and leetcode.get("total_submissions"):
            signals.append(f"LeetCode: {leetcode['total_submissions']} total submissions")
    else:
        signals.append(f"LeetCode not verified: {leetcode.get('reason')}")
    return round(min(score, 100.0), 1), signals


async def verify_candidate(
    candidate: dict[str, Any], *, keywords: list[str], enrich: bool = False
) -> dict[str, Any]:
    """Run both verifications concurrently and package the evidence.

    When ``enrich`` is set and a personal website/portfolio is on file, that page
    is fetched once so any GitHub/LeetCode links it exposes can be verified even
    when the resume itself did not list them. Without ``enrich`` this behaves as
    before (used by the batch evaluation path, which must stay fast).
    """
    github_url = candidate.get("github_url")
    leetcode_url = candidate.get("leetcode_url")

    # The portfolio column may in fact hold a social URL (resume extraction can
    # mislabel it); classify it before deciding whether to crawl.
    website_url = candidate.get("portfolio_url")
    if website_url:
        kind = profile_links.classify_url(website_url)
        if kind == "github" and not github_url:
            github_url = website_url
        elif kind == "leetcode" and not leetcode_url:
            leetcode_url = website_url

    website: dict[str, Any] | None = None
    discovered: dict[str, str] = {}
    if enrich and website_url and (not github_url or not leetcode_url):
        website = await profile_links.fetch_website(website_url)
        if website.get("available"):
            discovered = profile_links.profiles_from_links(website.get("links") or {})
            github_url = github_url or discovered.get("github_url")
            leetcode_url = leetcode_url or discovered.get("leetcode_url")

    github, leetcode = await asyncio.gather(
        verify_github(github_url, keywords=keywords),
        verify_leetcode(leetcode_url),
    )
    # Record when a URL was learned by crawling the personal website, so the UI
    # can show where the evidence came from.
    if discovered.get("github_url") and github_url == discovered["github_url"]:
        github["discovered_from"] = website_url
    if discovered.get("leetcode_url") and leetcode_url == discovered["leetcode_url"]:
        leetcode["discovered_from"] = website_url

    score, signals = evidence_score(github, leetcode)
    if website is not None:
        if website.get("available"):
            note = f"Personal website checked: {website.get('final_url') or website_url}"
            if discovered:
                note += " (discovered " + ", ".join(sorted(discovered)) + ")"
            signals.append(note)
        else:
            signals.append(f"Personal website not checked: {website.get('reason')}")

    result: dict[str, Any] = {
        "github": github,
        "leetcode": leetcode,
        "evidence_score": score,
        "signals": signals,
        "verified_at": _now(),
    }
    if website is not None:
        result["website"] = website
        result["discovered"] = discovered
    return result
