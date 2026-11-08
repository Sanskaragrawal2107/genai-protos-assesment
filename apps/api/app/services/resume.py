"""Resume text extraction and structured parsing.

Two extraction strategies:

* ``llm``       – structured JSON output validated against ``ResumeExtraction``.
* ``heuristic`` – deterministic rules used when no LLM key is configured or the
  provider fails. This keeps ingestion functional (and testable) offline.

Both strategies obey the same rule: a field is ``None`` when the resume does not
state it. Nothing is inferred or invented.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from ..db import utcnow
from ..llm import LLMError, LLMUnavailable, llm, wrap_document
from ..schemas import ResumeEducation, ResumeExtraction, ResumeProject
from .profile_links import classify_url

log = logging.getLogger("recruiter.resume")

SUPPORTED_EXTENSIONS = {"pdf", "docx", "doc"}


def normalize_profile_urls(data: ResumeExtraction) -> ResumeExtraction:
    """Place each extracted URL in its canonical field by host.

    Extraction can mislabel: a GitHub link recorded as ``portfolio_url``, or a
    personal site recorded as ``github_url``. Classifying by host keeps the
    canonical columns correct and reserves ``portfolio_url`` for the personal
    website, which verification then crawls for any missing profiles.
    """
    by_kind: dict[str, str] = {}
    for url in (data.github_url, data.leetcode_url, data.linkedin_url, data.portfolio_url):
        if not url:
            continue
        by_kind.setdefault(classify_url(url), url)
    data.github_url = by_kind.get("github")
    data.leetcode_url = by_kind.get("leetcode")
    data.linkedin_url = by_kind.get("linkedin")
    data.portfolio_url = by_kind.get("website")
    return data


class ResumeParseError(RuntimeError):
    pass


# --------------------------------------------------------------------- text
def extract_text(data: bytes, filename: str) -> tuple[str, str]:
    """Return ``(text, method)`` for a resume blob."""
    ext = (filename.rsplit(".", 1)[-1] or "").lower() if "." in filename else ""
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ResumeParseError("PDF support is not installed") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - malformed PDF from the wild
            raise ResumeParseError(f"Could not read PDF: {exc.__class__.__name__}") from exc
        text = "\n".join(pages).strip()
        if len(text) < 40:
            return text, "pdf_no_text"  # scanned/image PDF → OCR required
        return text, "pdf"
    if ext == "docx":
        try:
            import docx
        except ImportError as exc:  # pragma: no cover
            raise ResumeParseError("DOCX support is not installed") from exc
        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise ResumeParseError(f"Could not read DOCX: {exc.__class__.__name__}") from exc
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts).strip(), "docx"
    if ext == "doc":
        # Legacy binary .doc cannot be parsed reliably without LibreOffice.
        # Recover what we can from the raw stream rather than failing silently.
        raw = data.decode("latin-1", errors="ignore")
        chunks = re.findall(r"[ -~]{12,}", raw)
        text = "\n".join(chunk.strip() for chunk in chunks if len(chunk.strip()) > 12)
        if len(text) < 60:
            raise ResumeParseError("Legacy .doc could not be read; convert it to PDF or DOCX")
        return text, "doc"
    raise ResumeParseError(f"Unsupported resume format: .{ext or 'unknown'}")


# ---------------------------------------------------------------- heuristics
_LANGUAGES = {
    "python", "java", "javascript", "typescript", "c++", "c#", "c", "go", "golang", "rust",
    "ruby", "php", "swift", "kotlin", "scala", "r", "matlab", "sql", "bash", "perl", "dart",
    "elixir", "haskell", "objective-c", "solidity",
}
_FRAMEWORKS = {
    "fastapi", "flask", "django", "express", "nestjs", "next.js", "nextjs", "react", "angular",
    "vue", "svelte", "spring", "spring boot", "rails", "laravel", ".net", "pytorch",
    "tensorflow", "langchain", "langgraph", "llamaindex", "haystack", "streamlit", "gradio",
    "tailwind", "tailwindcss", "redux", "graphql", "apollo", "prisma", "sqlalchemy",
}
_TOOLS = {
    "docker", "kubernetes", "aws", "gcp", "azure", "postgresql", "postgres", "mysql",
    "mongodb", "redis", "kafka", "rabbitmq", "celery", "airflow", "spark", "hadoop", "git",
    "github", "gitlab", "jenkins", "terraform", "supabase", "firebase", "pinecone",
    "weaviate", "qdrant", "chromadb", "clickhouse", "elasticsearch", "grafana", "prometheus",
    "linux", "nginx", "jenkins", "mlflow", "wandb", "huggingface", "hugging face", "vercel",
    "microservices", "ci/cd", "rag", "llm", "openai", "gemini", "anthropic",
}
_EXTRA_SKILLS = {
    "machine learning", "deep learning", "nlp", "computer vision", "data science",
    "data engineering", "mlops", "system design", "distributed systems", "rest api",
    "rest apis", "api design", "concurrency", "testing", "unit testing", "agile", "scrum",
    "product management", "team leadership", "mentoring", "etl", "data modeling",
}
_KNOWN = _LANGUAGES | _FRAMEWORKS | _TOOLS | _EXTRA_SKILLS

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}[\s\-.]?)?(?:\(?\d{2,5}\)?[\s\-.]?){2,5}\d{2,4}")
_URL_RE = re.compile(r"https?://[^\s,;)\]}>\"']+", re.IGNORECASE)
# Bare domains such as ``janedoe.dev`` or ``www.janedoe.dev``. Each label must
# be at least two characters, and extraction only accepts lowercase matches, so
# degree/technology names like "B.Tech" or "ASP.NET" are not treated as sites.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.\-/])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])\.)+"
    r"(?:com|dev|io|tech|me|app|ai|xyz|net|org|in|co|site|page|design|studio|cloud|systems|online|website|space)"
    r"(?:/[^\s,;)\]}>\"']*)?",
    re.IGNORECASE,
)
_FILE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".svg", ".webp",
    ".zip", ".ppt", ".pptx",
)
_PERSONAL_TLDS = (
    ".dev", ".tech", ".me", ".app", ".io", ".ai", ".xyz", ".site", ".page",
    ".design", ".studio", ".online", ".website", ".space",
)


def _extract_urls(text: str) -> list[str]:
    """Return full http(s) URLs plus bare personal domains found in the text."""
    urls = [url.rstrip(".,;:") for url in _URL_RE.findall(text)]
    seen = {url.rstrip("/").lower() for url in urls}
    for match in _BARE_DOMAIN_RE.finditer(text):
        domain = match.group(0).rstrip(".,;:")
        # Reject mixed-case hosts (B.Tech, ASP.NET) unless explicitly www. The
        # path may be mixed case (e.g. github.com/Sanskaragrawal2107).
        host = domain.split("/", 1)[0]
        if host != host.lower() and not host.lower().startswith("www."):
            continue
        key = domain.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(f"https://{domain}")
    return urls


def _pick_portfolio(urls: list[str], full_name: str | None) -> str | None:
    """Pick the most likely personal site from a resume's non-social URLs."""
    candidates = [
        url
        for url in urls
        if classify_url(url) == "website"
        and not url.lower().split("?", 1)[0].endswith(_FILE_SUFFIXES)
    ]
    if not candidates:
        return None
    tokens = {t for t in re.findall(r"[a-z0-9]+", (full_name or "").lower()) if len(t) > 2}

    def score(url: str) -> int:
        lowered = url.lower().split("?", 1)[0].rstrip("/")
        value = 0
        if tokens and any(token in lowered for token in tokens):
            value += 2
        if lowered.endswith(_PERSONAL_TLDS):
            value += 1
        return value

    return max(candidates, key=score)
_SECTION_WORDS = {
    "summary", "profile", "objective", "experience", "work experience", "professional experience",
    "employment", "education", "skills", "technical skills", "projects", "certifications",
    "achievements", "awards", "publications", "languages", "interests", "contact",
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RANGE_RE = re.compile(
    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*)?(\d{4})"
    r"\s*(?:-|–|—|to|until)\s*"
    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*)?(\d{4}|present|current)",
    re.IGNORECASE,
)


def _clean_lines(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _guess_name(lines: list[str]) -> str | None:
    for line in lines[:8]:
        if "@" in line or "http" in line.lower():
            continue
        if re.search(r"\d", line):
            continue
        words = line.split()
        if 1 < len(words) <= 4 and all(re.match(r"^[A-Za-z][A-Za-z'.\-]*$", w) for w in words):
            if line.lower().strip(" :") in _SECTION_WORDS:
                continue
            if line.isupper() and len(words) == 1:
                continue
            return " ".join(w.capitalize() if w.islower() else w for w in words)
    return None


_CITY_RE = re.compile(
    r"\b(india|bangalore|bengaluru|mumbai|delhi|noida|gurgaon|hyderabad|pune|chennai|kolkata|"
    r"london|manchester|berlin|amsterdam|paris|madrid|new york|san francisco|seattle|austin|"
    r"boston|toronto|vancouver|remote|singapore|dubai|sydney|tokyo)\b",
    re.IGNORECASE,
)


def _guess_location(lines: list[str]) -> str | None:
    for line in lines[:20]:
        match = re.search(r"(?:location|based in|address|residing)\s*[:\-]\s*(.+)", line, re.IGNORECASE)
        if match:
            return match.group(1).strip(" .|")
    for line in lines[:20]:
        if not _CITY_RE.search(line):
            continue
        for segment in re.split(r"\s*\|\s*", line):
            if _CITY_RE.search(segment) and len(segment) <= 50 and "@" not in segment:
                if re.search(r"\d{4,}", segment):  # phone/zip noise
                    continue
                return segment.strip(" .,:")
    return None


def _experience_years(text: str) -> float | None:
    explicit = re.search(
        r"(?:\d+(?:\.\d+)?\s*(?:years|yrs)\s+(?:of\s+)?(?:relevant\s+|total\s+)?experience)"
        r"|(?:(?:total\s+|relevant\s+)?experience\s*[:\-]\s*(\d+(?:\.\d+)?)\+?\s*(?:years|yrs))",
        text,
        re.IGNORECASE,
    )
    if explicit:
        value = next((group for group in explicit.groups() if group), None)
        if value is None:
            head = re.match(r"(\d+(?:\.\d+)?)", explicit.group(0))
            value = head.group(1) if head else None
        if value:
            try:
                return round(float(value), 1)
            except ValueError:
                pass

    spans: list[tuple[int, int]] = []
    now_year = utcnow().year
    for match in _DATE_RANGE_RE.finditer(text):
        smonth, syear, emonth, eyear = match.groups()
        try:
            start_year = int(syear)
        except (TypeError, ValueError):
            continue
        start_month = _MONTHS.get((smonth or "jan").lower()[:4], None) or _MONTHS.get((smonth or "jan").lower()[:3], 1)
        if eyear and eyear.lower() in {"present", "current"}:
            end_year, end_month = now_year, utcnow().month
        elif eyear:
            try:
                end_year = int(eyear)
            except ValueError:
                continue
            end_month = _MONTHS.get((emonth or "dec").lower()[:4], None) or _MONTHS.get((emonth or "dec").lower()[:3], 12)
        else:
            continue
        if end_year < start_year or start_year < 1990 or end_year > now_year + 1:
            continue
        start_idx = start_year * 12 + (start_month or 1)
        end_idx = end_year * 12 + (end_month or 12)
        if end_idx > start_idx:
            spans.append((start_idx, end_idx))
    if not spans:
        return None
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    months = sum(end - start for start, end in merged)
    return round(months / 12, 1) if months > 0 else None


def _sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = "header"
    sections[current] = []
    for line in lines:
        key = line.strip(" :•-").lower()
        is_heading = (
            key in _SECTION_WORDS
            or (line.isupper() and 3 < len(line) < 40 and not re.search(r"\d", line))
        )
        if is_heading and key in _SECTION_WORDS | {"header"}:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _skills(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    lowered = text.lower()
    found: list[str] = []
    for keyword in sorted(_KNOWN, key=len, reverse=True):
        pattern = r"(?<![a-z0-9+#.])" + re.escape(keyword) + r"(?![a-z0-9+#])"
        if re.search(pattern, lowered):
            found.append(keyword)
    canonical = {"golang": "Go", "nextjs": "Next.js", "postgres": "PostgreSQL", "tailwindcss": "Tailwind CSS",
                 "hugging face": "Hugging Face", "rest apis": "REST APIs", "ci/cd": "CI/CD", "llm": "LLM",
                 "rag": "RAG", "nlp": "NLP", "mlops": "MLOps"}
    display = sorted({canonical.get(s, s.title() if s.islower() and len(s) > 3 else s) for s in found})
    by_lower = {s.lower(): s for s in display}
    languages = [by_lower[s] for s in _LANGUAGES if s in by_lower]
    frameworks = [by_lower[s] for s in _FRAMEWORKS if s in by_lower]
    tools = [by_lower[s] for s in _TOOLS if s in by_lower]
    return display, languages, frameworks, tools


def _projects(lines: list[str]) -> list[ResumeProject]:
    projects: list[ResumeProject] = []
    for line in lines:
        match = re.match(r"^[•\-\*]?\s*([A-Z][\w .:/&'()\-]{2,60}?)\s*(?:[:\-–—]|\s{2,})\s*(.+)$", line)
        if match:
            title, description = match.group(1).strip(), match.group(2).strip()
        else:
            match = re.match(r"^[•\-\*]\s*(.+)$", line)
            if not match:
                continue
            title, description = None, match.group(1).strip()
        projects.append(
            ResumeProject(
                name=title,
                description=description[:1200],
                technologies=[t for t in re.findall(r"\b[A-Z][A-Za-z+#.]{1,15}\b", description or "")][:8],
            )
        )
        if len(projects) >= 12:
            break
    return projects


def _education(lines: list[str]) -> tuple[list[ResumeEducation], str | None, int | None]:
    entries: list[ResumeEducation] = []
    university = None
    graduation_year = None
    degree_re = re.compile(r"(b\.?tech|b\.?e\b|m\.?tech|b\.?sc|m\.?sc|bca|mca|mba|ph\.?d|bachelor|master|diploma)",
                           re.IGNORECASE)
    inst_re = re.compile(r"(university|institute|college|school of|iit|nit|bits)", re.IGNORECASE)
    for line in lines:
        year_match = re.search(r"(19|20)\d{2}", line)
        if degree_re.search(line) or inst_re.search(line):
            degree = degree_re.search(line)
            institution = None
            for part in re.split(r"[|,•]", line):
                if inst_re.search(part):
                    institution = part.strip()
                    break
            entry = ResumeEducation(
                degree=degree.group(0).title() if degree else None,
                institution=institution,
                graduation_year=int(year_match.group(0)) if year_match else None,
            )
            if entry.degree or entry.institution:
                entries.append(entry)
            if institution and not university:
                university = institution
            if year_match and not graduation_year:
                graduation_year = int(year_match.group(0))
    return entries[:6], university, graduation_year


def _role(lines: list[str]) -> tuple[str | None, str | None]:
    role = None
    company = None
    for line in lines[:25]:
        match = re.match(r"^[•\-\*]?\s*([A-Z][A-Za-z /&+.#-]{2,45}?)\s+(?:at|@)\s+([A-Z][\w .&'-]{1,45})$", line)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        match = re.match(r"^(?:current(?:ly)?\s+(?:role|title)|designation|role|title)\s*[:\-]\s*(.+)$",
                         line, re.IGNORECASE)
        if match and not role:
            value = match.group(1).split("|")[0].strip(" .")
            role = value
        match = re.match(r"^(?:company|organization|employer)\s*[:\-]\s*(.+)$", line, re.IGNORECASE)
        if match and not company:
            company = match.group(1).split("|")[0].strip(" .")
    return role, company


def heuristic_extract(text: str, *, email_hint: str | None = None) -> ResumeExtraction:
    lines = _clean_lines(text)
    sections = _sections(lines)
    email_match = _EMAIL_RE.search(text)
    phone_match = None
    for line in lines[:25]:
        if "phone" in line.lower() or "mobile" in line.lower() or "+" in line:
            found = _PHONE_RE.search(line)
            if found and len(re.sub(r"\D", "", found.group(0))) >= 8:
                phone_match = found
                break
    urls = _extract_urls(text)
    github = next((u for u in urls if "github.com" in u.lower()), None)
    leetcode = next((u for u in urls if "leetcode.com" in u.lower()), None)
    linkedin = next((u for u in urls if "linkedin.com" in u.lower()), None)
    full_name = _guess_name(lines)
    portfolio = _pick_portfolio(urls, full_name)
    skills, languages, frameworks, tools = _skills(text)
    education, university, grad_year = _education(sections.get("education") or lines)
    project_lines = sections.get("projects") or []
    role, company = _role(lines)
    summary_lines = sections.get("summary") or sections.get("profile") or []

    extracted = ResumeExtraction(
        full_name=full_name,
        email=email_match.group(0) if email_match else (email_hint or None),
        phone=phone_match.group(0).strip() if phone_match else None,
        location=_guess_location(lines),
        preferred_location=None,
        experience_years=_experience_years(text),
        current_role=role,
        current_company=company,
        previous_companies=[],
        education=education,
        university=university,
        graduation_year=grad_year,
        skills=skills,
        programming_languages=languages,
        frameworks=frameworks,
        tools=tools,
        projects=_projects(project_lines),
        summary=" ".join(summary_lines)[:600] or None,
        github_url=github,
        leetcode_url=leetcode,
        linkedin_url=linkedin,
        portfolio_url=portfolio,
    )
    return normalize_profile_urls(extracted)


# -------------------------------------------------------------------- LLM
_SYSTEM = (
    "You are a resume information extraction engine for a recruiting system. "
    "Return ONLY a JSON object matching the requested schema. Use null for any "
    "field the resume does not explicitly state. Never guess a phone number, a "
    "company, a graduation year, a URL or a number of years of experience. "
    "Normalise skill names to their common spelling. Keep experience_years as a "
    "decimal number computed from stated employment dates only. "
    "Place URLs in the correct field by kind: a GitHub profile in github_url, a "
    "LeetCode profile in leetcode_url, a LinkedIn profile in linkedin_url, and any "
    "personal website, portfolio or other personal link in portfolio_url. Never "
    "put a social profile URL in portfolio_url."
)

_SCHEMA_HINT = """{
  "full_name": string|null, "email": string|null, "phone": string|null,
  "location": string|null, "preferred_location": string|null,
  "experience_years": number|null, "current_role": string|null, "current_company": string|null,
  "previous_companies": string[], "education": [{"degree": string|null, "institution": string|null, "graduation_year": number|null, "field": string|null}],
  "university": string|null, "graduation_year": number|null,
  "skills": string[], "programming_languages": string[], "frameworks": string[], "tools": string[],
  "projects": [{"name": string|null, "description": string|null, "technologies": string[], "url": string|null}],
  "summary": string|null, "github_url": string|null, "leetcode_url": string|null,
  "linkedin_url": string|null, "portfolio_url": string|null
}"""


@dataclass(slots=True)
class ParsedResume:
    data: ResumeExtraction
    method: str
    confidence: float
    warnings: list[str]


def _confidence(data: ResumeExtraction) -> float:
    checks = [
        bool(data.full_name),
        bool(data.email),
        bool(data.phone),
        bool(data.skills),
        data.experience_years is not None,
        bool(data.education) or bool(data.university),
        bool(data.projects),
    ]
    return round(sum(1 for c in checks if c) / len(checks), 2)


async def parse_resume(text: str, *, filename: str, email_hint: str | None = None) -> ParsedResume:
    if not text or len(text.strip()) < 20:
        raise ResumeParseError("Resume contains no extractable text")

    if not llm.enabled:
        raise ResumeParseError("LLM is required for resume parsing but is not enabled")

    prompt = (
        "Extract the candidate fields from the resume below.\n"
        f"Sender email (fallback only if the resume has no email): {email_hint or 'unknown'}\n"
        f"Filename: {filename}\n"
        f"Schema: {_SCHEMA_HINT}\n\n"
        + wrap_document(text, source=f"resume:{filename}")
    )
    try:
        raw = await llm.complete_json(system=_SYSTEM, user=prompt)
        data = normalize_profile_urls(ResumeExtraction.model_validate(raw))
        return ParsedResume(data=data, method="llm", confidence=_confidence(data), warnings=[])
    except (LLMUnavailable, LLMError, ValueError) as exc:
        log.error("LLM resume extraction failed: %s", exc)
        raise ResumeParseError(f"LLM extraction failed: {exc}") from exc
