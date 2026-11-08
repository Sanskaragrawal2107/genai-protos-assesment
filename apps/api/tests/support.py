"""Test fixtures: sample resumes and an in-memory mailbox.

These live in the test suite (not in ``app/``) because the production service
has no local mode. The bytes generated here are real PDF/DOCX files, so the
parsing path exercised is the same production path.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.composio import IntegrationError
from app.services.mail import AttachmentMeta, MailMessage


@dataclass
class SampleAttachment:
    attachment_id: str
    filename: str
    mime_type: str
    data: bytes


@dataclass
class SampleEmail:
    message_id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    received_at: datetime
    attachments: list[SampleAttachment] = field(default_factory=list)


# ------------------------------------------------------------------ builders
def make_pdf(lines: list[str]) -> bytes:
    """Minimal, valid single-page PDF with Helvetica text (no dependencies)."""
    escaped: list[str] = []
    for line in lines:
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        escaped.append(safe[:110])

    content = ["BT", "/F1 11 Tf", "14 TL", "56 770 Td"]
    for line in escaped:
        content.append(f"({line}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", errors="replace")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(out)


def make_docx(lines: list[str]) -> bytes:
    import docx

    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _resume_lines(
    *,
    name: str,
    email: str,
    phone: str,
    location: str,
    role: str,
    company: str,
    experience: str,
    skills: str,
    summary: str,
    projects: list[tuple[str, str]],
    education: str,
    links: list[str],
) -> list[str]:
    lines = [
        name,
        f"{role} at {company}",
        f"{email} | {phone} | {location}",
        "",
        "SUMMARY",
        summary,
        "",
        "TECHNICAL SKILLS",
        skills,
        "",
        "PROFESSIONAL EXPERIENCE",
        f"{role} - {company} | 2021 - Present",
        "Built and operated production systems, owned reliability and mentored engineers.",
        f"Total experience: {experience} years",
        "",
        "PROJECTS",
    ]
    for title, description in projects:
        lines.append(f"{title}: {description}")
    lines += ["", "EDUCATION", education, "", "LINKS", " | ".join(links)]
    return lines


def sample_emails() -> list[SampleEmail]:
    now = datetime.now(timezone.utc)

    priya = _resume_lines(
        name="Priya Nair",
        email="priya.nair@example.com",
        phone="+91 98765 43210",
        location="Bengaluru, India",
        role="Senior AI Engineer",
        company="Northwind Labs",
        experience="4",
        skills="Python, FastAPI, LangGraph, LangChain, RAG, PostgreSQL, Docker, AWS, Redis, LLM",
        summary="AI engineer focused on retrieval augmented generation and agentic workflows in production.",
        projects=[
            ("Recruitment Copilot", "built a LangGraph agent that screens 2,000 resumes per day using FastAPI and pgvector"),
            ("Support Triage RAG", "retrieval augmented generation service handling 40k tickets monthly on AWS"),
        ],
        education="B.Tech Computer Science | BMS College of Engineering | 2019",
        links=["https://github.com/octocat", "https://leetcode.com/leetcode", "https://linkedin.com/in/priyanair"],
    )
    arjun = _resume_lines(
        name="Arjun Mehta",
        email="arjun.mehta@example.com",
        phone="+91 90000 12345",
        location="Pune, India",
        role="Backend Engineer",
        company="Finlytic",
        experience="3",
        skills="Python, Django, PostgreSQL, Redis, Celery, Docker, Kubernetes, REST APIs",
        summary="Backend engineer building payments infrastructure with strict reliability requirements.",
        projects=[
            ("Ledger Service", "double-entry ledger processing 1.2M transactions per month in Django"),
            ("Queue Hardening", "migrated Celery workloads to Kubernetes with zero downtime"),
        ],
        education="B.E Information Technology | Pune Institute of Computer Technology | 2020",
        links=["https://github.com/octocat", "https://linkedin.com/in/arjunmehta"],
    )
    sana = _resume_lines(
        name="Sana Kapoor",
        email="sana.kapoor@example.com",
        phone="+44 7700 900123",
        location="London, United Kingdom",
        role="Machine Learning Engineer",
        company="Helio Health",
        experience="6",
        skills="Python, PyTorch, TensorFlow, MLOps, NLP, Hugging Face, Kubernetes, GCP, Airflow",
        summary="ML engineer shipping clinical NLP models with strong MLOps practice.",
        projects=[
            ("Clinical Note Tagger", "transformer NLP pipeline with 92 percent F1 in production"),
            ("MLOps Platform", "Airflow and Kubernetes based training platform for 30 researchers"),
        ],
        education="M.Sc Machine Learning | University College London | 2018",
        links=["https://github.com/octocat", "https://leetcode.com/leetcode"],
    )

    return [
        SampleEmail(
            message_id="sample-msg-1001",
            thread_id="sample-thread-1001",
            sender_name="Priya Nair",
            sender_email="priya.nair@example.com",
            subject="Application: Senior AI Engineer - Priya Nair",
            body=(
                "Hi team,\n\nI am applying for the Senior AI Engineer role. My resume is attached.\n"
                "I have four years of experience building production RAG and LangGraph systems.\n\n"
                "Best,\nPriya"
            ),
            received_at=now - timedelta(minutes=25),
            attachments=[
                SampleAttachment("sample-att-1", "Priya_Nair_Resume.pdf", "application/pdf", make_pdf(priya))
            ],
        ),
        SampleEmail(
            message_id="sample-msg-1002",
            thread_id="sample-thread-1002",
            sender_name="Arjun Mehta",
            sender_email="arjun.mehta@example.com",
            subject="Backend Engineer application - resume attached",
            body="Hello, please find my resume attached for the Backend Engineer opening. Regards, Arjun",
            received_at=now - timedelta(hours=2),
            attachments=[
                SampleAttachment(
                    "sample-att-2",
                    "Arjun_Mehta_Resume.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    make_docx(arjun),
                )
            ],
        ),
        SampleEmail(
            message_id="sample-msg-1003",
            thread_id="sample-thread-1003",
            sender_name="Sana Kapoor",
            sender_email="sana.kapoor@example.com",
            subject="ML Engineer - Sana Kapoor",
            body="Hi, I would like to be considered for the ML Engineer position. Resume attached.",
            received_at=now - timedelta(hours=5),
            attachments=[
                SampleAttachment("sample-att-3", "Sana_Kapoor_Resume.pdf", "application/pdf", make_pdf(sana))
            ],
        ),
        SampleEmail(
            message_id="sample-msg-1004",
            thread_id="sample-thread-1004",
            sender_name="Newsletter",
            sender_email="news@techdigest.example.com",
            subject="This week in AI",
            body="Top stories in AI this week.",
            received_at=now - timedelta(hours=6),
            attachments=[
                SampleAttachment("sample-att-4", "banner.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            ],
        ),
    ]


class SampleMailbox:
    """In-memory ``MailProvider`` used only by the test suite."""

    name = "gmail"

    def __init__(self) -> None:
        self._emails = sample_emails()
        self.outbox: list[dict[str, Any]] = []

    async def list_messages(
        self, *, since: datetime | None, query: str, max_results: int = 25
    ) -> list[MailMessage]:
        out: list[MailMessage] = []
        for email in self._emails:
            if since is not None and email.received_at < since:
                continue
            out.append(
                MailMessage(
                    provider_message_id=email.message_id,
                    thread_id=email.thread_id,
                    sender_email=email.sender_email,
                    sender_name=email.sender_name,
                    recipients=["recruiting@example.com"],
                    subject=email.subject,
                    snippet=email.body[:200],
                    body_text=email.body,
                    received_at=email.received_at,
                    attachments=[
                        AttachmentMeta(a.attachment_id, a.filename, a.mime_type, len(a.data))
                        for a in email.attachments
                    ],
                    headers={"labels": ["INBOX"]},
                )
            )
        return out[:max_results]

    async def download_attachment(self, message: MailMessage, attachment: AttachmentMeta) -> bytes:
        for email in self._emails:
            if email.message_id != message.provider_message_id:
                continue
            for item in email.attachments:
                if item.attachment_id == attachment.attachment_id:
                    return item.data
        raise IntegrationError("Sample attachment not found", provider="gmail", code="not_found")

    async def send_email(self, *, to: str, subject: str, body: str, is_html: bool = False) -> dict[str, Any]:
        self.outbox.append({"to": to, "subject": subject, "body": body, "is_html": is_html})
        return {"provider_message_id": f"test-{len(self.outbox)}", "thread_id": None, "raw_status": "sent"}


class InMemoryStorage:
    """Stand-in for Supabase Storage so ingestion tests need no network."""

    backend = "supabase"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, path: str, data: bytes, content_type: str) -> str:
        self.objects[path] = data
        return path

    async def get(self, path: str) -> bytes:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]

    async def delete(self, path: str) -> None:
        self.objects.pop(path, None)


SAMPLE_JOB_DESCRIPTION = """Senior AI Engineer - Recruitment Intelligence

We are hiring a Senior AI Engineer to build agentic recruiting workflows.

Responsibilities
- Design and ship retrieval augmented generation (RAG) pipelines over candidate data
- Build LangGraph or LangChain agent workflows and expose them through FastAPI
- Own Postgres schema design and query performance
- Deploy and operate services on AWS with Docker

Required skills
- Python (4+ years)
- FastAPI
- LangGraph or LangChain
- RAG / vector search
- PostgreSQL

Preferred skills
- TypeScript and React
- Docker and Kubernetes
- LLM evaluation and prompt engineering

Requirements
- Minimum 3 years of professional software engineering experience
- Bachelor's degree in Computer Science or equivalent practical experience
- Location: Bengaluru or remote within India
"""
