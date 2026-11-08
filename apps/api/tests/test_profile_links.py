import asyncio

from app.config import settings
from app.schemas import ResumeExtraction
from app.services import verification
from app.services.profile_links import (
    classify_url,
    is_public_url,
    parse_website,
    profiles_from_links,
)
from app.services.resume import normalize_profile_urls


def test_classify_url_by_host():
    assert classify_url("https://github.com/torvalds") == "github"
    assert classify_url("https://www.github.com/torvalds") == "github"
    assert classify_url("https://leetcode.com/u/neal_wu/") == "leetcode"
    assert classify_url("https://linkedin.com/in/jane") == "linkedin"
    assert classify_url("https://janedoe.dev") == "website"
    assert classify_url(None) == "website"


def test_is_public_url_blocks_private_and_schemes():
    assert is_public_url("https://example.com") is True
    assert is_public_url("http://localhost:3000") is False
    assert is_public_url("http://127.0.0.1/x") is False
    assert is_public_url("http://10.0.0.5/x") is False
    assert is_public_url("http://169.254.169.254/latest/meta-data") is False
    assert is_public_url("ftp://example.com") is False
    assert is_public_url("javascript:alert(1)") is False
    assert is_public_url(None) is False


def test_parse_website_groups_and_dedupes_links():
    html = """
    <html><head><title>  Jane Doe  </title>
    <meta name="description" content="Backend engineer"></head>
    <body>
      <a href="/resume.pdf">Resume</a>
      <a href="https://github.com/janedoe">GitHub</a>
      <a href="https://github.com/janedoe">dup</a>
      <a href="https://leetcode.com/u/janedoe/">LeetCode</a>
      <a href="https://linkedin.com/in/janedoe">LinkedIn</a>
      <a href="mailto:jane@example.com">mail</a>
      <a href="#top">top</a>
      <a href="http://127.0.0.1/secret">nope</a>
    </body></html>
    """
    parsed = parse_website(html, "https://janedoe.dev/")
    assert parsed["title"] == "Jane Doe"
    assert parsed["description"] == "Backend engineer"
    assert parsed["links"]["github"] == ["https://github.com/janedoe"]
    assert parsed["links"]["leetcode"] == ["https://leetcode.com/u/janedoe/"]
    assert parsed["links"]["linkedin"] == ["https://linkedin.com/in/janedoe"]
    # relative + private links kept in the website bucket only when public
    assert "https://janedoe.dev/resume.pdf" in parsed["links"]["website"]
    assert all("127.0.0.1" not in link for links in parsed["links"].values() for link in links)

    found = profiles_from_links(parsed["links"])
    assert found["github_url"] == "https://github.com/janedoe"
    assert found["leetcode_url"] == "https://leetcode.com/u/janedoe/"


def test_normalize_profile_urls_moves_mislabelled_urls():
    data = ResumeExtraction(
        full_name="Jane Doe",
        github_url="https://janedoe.dev",          # actually a personal site
        portfolio_url="https://github.com/janedoe",  # actually GitHub
    )
    out = normalize_profile_urls(data)
    assert out.github_url == "https://github.com/janedoe"
    assert out.portfolio_url == "https://janedoe.dev"


def test_verify_candidate_enrich_discovers_from_website(monkeypatch):
    async def fake_fetch(url):
        return {
            "provider": "website",
            "available": True,
            "final_url": url,
            "title": "Jane Doe",
            "description": "Backend engineer",
            "links": {
                "github": ["https://github.com/janedoe"],
                "leetcode": ["https://leetcode.com/u/janedoe/"],
                "linkedin": [],
                "website": [],
            },
        }

    async def fake_github(url, *, keywords=None):
        return {"provider": "github", "available": True, "username": "janedoe", "public_repos": 5,
                "relevant_repositories": [], "profile_url": url}

    async def fake_leetcode(url):
        return {"provider": "leetcode", "available": True, "username": "janedoe", "total_solved": 42,
                "profile_url": url}

    monkeypatch.setattr(verification.profile_links, "fetch_website", fake_fetch)
    monkeypatch.setattr(verification, "verify_github", fake_github)
    monkeypatch.setattr(verification, "verify_leetcode", fake_leetcode)

    candidate = {
        "github_url": None,
        "leetcode_url": None,
        "portfolio_url": "https://janedoe.dev",
        "skills": ["python"],
    }
    result = asyncio.run(verification.verify_candidate(candidate, keywords=["python"], enrich=True))

    assert result["discovered"]["github_url"] == "https://github.com/janedoe"
    assert result["github"]["discovered_from"] == "https://janedoe.dev"
    assert result["leetcode"]["available"] is True
    assert result["website"]["title"] == "Jane Doe"
    assert any("Personal website checked" in s for s in result["signals"])


def test_verify_candidate_without_enrich_skips_website(monkeypatch):
    calls = {"website": 0}

    async def fake_fetch(url):  # should not be called
        calls["website"] += 1
        return {"available": True, "links": {}}

    async def fake_github(url, *, keywords=None):
        return {"provider": "github", "available": False, "reason": "No GitHub profile URL on the resume"}

    async def fake_leetcode(url):
        return {"provider": "leetcode", "available": False, "reason": "No LeetCode profile URL on the resume"}

    monkeypatch.setattr(verification.profile_links, "fetch_website", fake_fetch)
    monkeypatch.setattr(verification, "verify_github", fake_github)
    monkeypatch.setattr(verification, "verify_leetcode", fake_leetcode)

    candidate = {"github_url": None, "leetcode_url": None, "portfolio_url": "https://janedoe.dev"}
    result = asyncio.run(verification.verify_candidate(candidate, keywords=[], enrich=False))

    assert calls["website"] == 0
    assert "website" not in result


def test_normalize_url_upgrades_schemeless_hosts():
    from app.services.profile_links import normalize_url

    assert normalize_url("sanskaragrawal.tech") == "https://sanskaragrawal.tech"
    assert normalize_url("https://sanskaragrawal.tech/") == "https://sanskaragrawal.tech/"
    assert normalize_url("   ") is None
    assert normalize_url(None) is None


def test_parse_website_extracts_text_and_headings():
    html = """
    <html><head><title>Jane Doe</title></head>
    <body>
      <h1>Jane Doe</h1>
      <h2>Projects</h2>
      <p>Built Hire a Human with LangGraph.</p>
      <script>var x = 'secret-token';</script>
      <style>.a{color:red}</style>
    </body></html>
    """
    parsed = parse_website(html, "https://janedoe.dev/")
    assert parsed["headings"][0] == {"tag": "h1", "text": "Jane Doe"}
    assert any(h["text"] == "Projects" for h in parsed["headings"])
    assert "Built Hire a Human with LangGraph." in parsed["text"]
    assert "secret-token" not in parsed["text"]
    assert "color:red" not in parsed["text"]


def test_resume_extracts_bare_portfolio_domain_without_degree_noise():
    from app.services.resume import _extract_urls, _pick_portfolio

    text = "Sanskar Agrawal\nB.Tech CSE, ASP.NET developer\nPortfolio: sanskaragrawal.tech\n"
    urls = _extract_urls(text)
    assert "https://sanskaragrawal.tech" in urls
    assert not any("B.Tech" in u or "ASP.NET" in u for u in urls)
    assert _extract_urls("mail me at jane@example.com") == []
    assert _pick_portfolio(
        ["https://example.com/resume.pdf", "https://acropolis.in", "https://sanskaragrawal.tech"],
        "Sanskar Agrawal",
    ) == "https://sanskaragrawal.tech"
    # Mixed-case paths are fine as long as the host is lowercase.
    assert "https://github.com/Sanskaragrawal2107" in _extract_urls(
        "GitHub: github.com/Sanskaragrawal2107"
    )


def test_verify_candidate_always_reads_website_when_enrich(monkeypatch):
    fetched = {"n": 0}

    async def fake_fetch(url):
        fetched["n"] += 1
        return {
            "available": True,
            "final_url": url,
            "title": "Jane Doe",
            "description": None,
            "headings": [{"tag": "h2", "text": "Projects"}],
            "text": "Hire a Human — LangGraph",
            "links": {"github": [], "leetcode": [], "linkedin": [], "website": []},
        }

    async def fake_github(url, *, keywords=None):
        return {"provider": "github", "available": False, "reason": "down"}

    async def fake_leetcode(url):
        return {"provider": "leetcode", "available": False, "reason": "down"}

    monkeypatch.setattr(verification.profile_links, "fetch_website", fake_fetch)
    monkeypatch.setattr(verification, "verify_github", fake_github)
    monkeypatch.setattr(verification, "verify_leetcode", fake_leetcode)

    candidate = {
        "github_url": "https://github.com/janedoe",
        "leetcode_url": "https://leetcode.com/u/janedoe/",
        "portfolio_url": "https://janedoe.dev",
    }
    result = asyncio.run(verification.verify_candidate(candidate, keywords=[], enrich=True))

    # GitHub/LeetCode are already known, but the page is still read.
    assert fetched["n"] == 1
    assert result["website"]["text"] == "Hire a Human — LangGraph"
    assert result["website"]["headings"][0]["text"] == "Projects"


def test_leetcode_graphql_is_primary_and_skips_mcp(monkeypatch):
    called = {"mcp": 0}

    async def fake_graphql(username):
        return {
            "provider": "leetcode",
            "source": "leetcode.com public GraphQL (unofficial endpoint)",
            "username": username,
            "available": True,
            "total_solved": 10,
        }

    async def fake_mcp(username):
        called["mcp"] += 1
        return {"available": True}

    from app.services import leetcode_mcp as lc_mcp

    monkeypatch.setattr(verification, "_verify_leetcode_graphql", fake_graphql)
    monkeypatch.setattr(lc_mcp, "fetch_leetcode_evidence", fake_mcp)
    monkeypatch.setattr(settings, "leetcode_mcp_url", "https://mcp.example/mcp")
    monkeypatch.setattr(settings, "leetcode_mcp_token", "token")
    monkeypatch.setattr(settings, "leetcode_mcp_fallback", True)

    out = asyncio.run(verification.verify_leetcode("https://leetcode.com/u/jane/"))
    assert out["available"] is True
    assert called["mcp"] == 0  # GraphQL answered, so MCP is never attempted


def test_leetcode_mcp_fallback_only_when_opted_in(monkeypatch):
    called = {"mcp": 0}

    async def fake_graphql(username):
        return {
            "provider": "leetcode",
            "source": "graphql",
            "username": username,
            "available": False,
            "reason": "public endpoint unavailable",
        }

    async def fake_mcp(username):
        called["mcp"] += 1
        return {"available": True, "source": "leetcode-mcp"}

    from app.services import leetcode_mcp as lc_mcp

    monkeypatch.setattr(verification, "_verify_leetcode_graphql", fake_graphql)
    monkeypatch.setattr(lc_mcp, "fetch_leetcode_evidence", fake_mcp)
    monkeypatch.setattr(settings, "leetcode_mcp_url", "https://mcp.example/mcp")
    monkeypatch.setattr(settings, "leetcode_mcp_token", "token")

    # Fallback disabled: GraphQL failure is final, MCP stays untouched.
    monkeypatch.setattr(settings, "leetcode_mcp_fallback", False)
    out = asyncio.run(verification.verify_leetcode("https://leetcode.com/u/jane/"))
    assert out["available"] is False
    assert called["mcp"] == 0

    # Opted in: MCP is consulted only because GraphQL failed, and it wins.
    monkeypatch.setattr(settings, "leetcode_mcp_fallback", True)
    out = asyncio.run(verification.verify_leetcode("https://leetcode.com/u/jane/"))
    assert out["available"] is True
    assert called["mcp"] == 1
    assert out["fallback_from"] == "graphql"

