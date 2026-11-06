"""Text cleaning, secret redaction and journal splitting (ported from RunbookIntelligence incidents/clean.py).

Everything that reaches storage or the LLM passes through clean_text() first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

NOTE_HEADER = re.compile(
    r"^(\d\d-\d\d-\d{4} \d\d:\d\d:\d\d|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) - (.+?) \((Work notes|Additional comments)\)[ \t]*\r?$",
    re.M,
)
AUTOMATED_AUTHORS = {"system", "guest", "artey", "arteycs"}
AUTOMATED_AUTHOR_HINTS = re.compile(r"xmatters|integration|\bapi\b", re.I)
MARKUP = re.compile(r"\[code\].*?\[/code\]|<[^>]+>", re.S | re.I)

# (name, pattern, replacement) — the name is also used by the secrets detector.
SECRETS = [
    ("credential_assignment",
     re.compile(r"\b(\w*(?:password|passwd|pwd|secret|token|api_?key))(\s*[:=]\s*)(?!\[REDACTED\])[^\s;,&\"']+", re.I),
     r"\1\2[REDACTED]"),
    ("url_userinfo", re.compile(r"(://)[^:/\s@]+:[^@\s]+@"), r"\1[REDACTED]@"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "[REDACTED-PRIVATE-KEY]"),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "[REDACTED-GH-TOKEN]"),
    ("bearer_token", re.compile(r"(\bBearer\s+)[A-Za-z0-9\-_\.=]{20,}", re.I), r"\1[REDACTED]"),
]


# Personal data removed from evidence before storage or the LLM (CMDB/CSDM V2 review: incident records carry
# email addresses). Kept apart from SECRETS so the secrets detector does not report them as leaked credentials.
PII = [
    ("email", re.compile(r"(?<![\w.+-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), "[REDACTED-EMAIL]"),
]


def redact_pii(text: str) -> str:
    for _, pattern, repl in PII:
        text = pattern.sub(repl, text)
    return text


def find_secrets(text: str) -> list[tuple[str, int]]:
    """(secret kind, 1-based line number) for each likely secret; values are never returned."""
    hits = []
    for name, pat, _ in SECRETS:
        for m in pat.finditer(text):
            hits.append((name, text.count("\n", 0, m.start()) + 1))
    return hits


def clean_text(text: str) -> str:
    text = MARKUP.sub(" ", text or "")
    for _, pattern, repl in SECRETS + PII:
        text = pattern.sub(repl, text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\r\n?", "\n", text)).strip()


def redact(text: str) -> str:
    """Secrets and personal data, keeping layout (for documents)."""
    for _, pattern, repl in SECRETS + PII:
        text = pattern.sub(repl, text)
    return text


_DT_FORMATS = ("%m-%d-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
               "%Y-%m-%dT%H:%M:%S.%fZ", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y")


def parse_datetime(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except ValueError:
        return None


@dataclass(frozen=True)
class NoteEntry:
    at: datetime | None
    author: str
    kind: str
    text: str
    by_person: bool


def is_automated(author: str) -> bool:
    a = author.strip().lower()
    return a in AUTOMATED_AUTHORS or bool(AUTOMATED_AUTHOR_HINTS.search(a))


def split_notes(text: str) -> list[NoteEntry]:
    headers = list(NOTE_HEADER.finditer(text or ""))
    if not headers:
        body = clean_text(text)
        return [NoteEntry(None, "unknown", "Work notes", body, True)] if body else []
    entries = []
    for h, nxt in zip(headers, headers[1:] + [None]):
        body = clean_text(text[h.end(): nxt.start() if nxt else len(text)])
        if body:
            author = h.group(2).strip()
            entries.append(NoteEntry(parse_datetime(h.group(1)), author, h.group(3), body, not is_automated(author)))
    entries.sort(key=lambda e: e.at or datetime.min)
    return entries


class Resolution(StrEnum):
    STEPS = "steps"
    AI_SUMMARY = "ai_summary"
    POINTER = "pointer"
    DUPLICATE = "duplicate"
    AI_FILLER = "ai_filler"
    FALSE_POSITIVE = "false_positive"
    THIN = "thin"
    EMPTY = "empty"


DUPLICATE = re.compile(r"closed as per incident|copied from parent incident|closed as the reference|^\s*INC\d+\.?\s*$", re.I)
AI_FILLER = re.compile(r"not explicitly (identified|stated|mentioned)|provided incident details|it can be inferred"
                       r"|as (indicated|noted) (by|in) the (system|activity stream)", re.I)
AI_SUMMARY = re.compile(r"^root cause:|resolution steps:", re.I)
POINTER = re.compile(r"as per (the )?(sd |on-?call )?(onenote|instructions?|sop|kb)|per (the )?onenote", re.I)
FALSE_POSITIVE = re.compile(r"false positive|no (action|resolution) (is )?(needed|required)", re.I)
GENERIC = re.compile(r"^(the )?(app|application) (is |was )?(back )?up and running|^(issue (is )?)?resolved|^fixed|^done"
                     r"|^closing|taken care|closed the task associated with alert|resolved incident from the portal"
                     r"|selected to resolve issue via", re.I)


def classify_resolution(close_notes: str) -> Resolution:
    text = clean_text(close_notes)
    words = len(text.split())
    if not words:
        return Resolution.EMPTY
    if DUPLICATE.search(text):
        return Resolution.DUPLICATE
    if AI_FILLER.search(text):
        return Resolution.AI_FILLER
    if POINTER.search(text) and words <= 25:
        return Resolution.POINTER
    if FALSE_POSITIVE.search(text) and words <= 12:
        return Resolution.FALSE_POSITIVE
    if words <= 5 or (GENERIC.search(text) and words <= 12):
        return Resolution.THIN
    if AI_SUMMARY.search(text):
        return Resolution.AI_SUMMARY
    return Resolution.STEPS
