"""Client-side helpers for Palimpsest authors and operators.

Sealing a claim locally is the only safe way to do it: sending the title, abstract,
elements and salt to any remote service (including an RPC node) before the reveal
would leak the claim and let someone else tailor a later claim around it.

The helpers normalise text exactly the way the contract does, so a commitment computed
here always matches what ``reveal_claim`` recomputes on-chain.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Iterable

COMMITMENT_DOMAIN = "palimpsest-claim-v1"
SALT_PATTERN = re.compile(r"[A-Za-z0-9_.~-]{16,64}")

MAX_TITLE = 120
MIN_ABSTRACT = 40
MAX_ABSTRACT = 1200
MIN_ELEMENTS = 2
MAX_ELEMENTS = 8
MIN_ELEMENT = 12
MAX_ELEMENT = 240

# Mirrors the contract: control characters (except tab, newline, carriage return),
# soft hyphen, zero-width and bidi-override characters, word joiners, BOM and
# interlinear annotation marks are refused.
HIDDEN = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u00ad\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb]"
)


def sha256_hex(raw: bytes) -> str:
    """0x-prefixed lowercase SHA-256, the format the contract expects."""
    return "0x" + hashlib.sha256(raw).hexdigest()


def new_salt() -> str:
    """A fresh 43-character URL-safe salt with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def clean(text: str) -> str:
    """Collapse all whitespace runs to single spaces, exactly like the contract."""
    return " ".join(str(text).replace("\x00", " ").split())


def normalize_elements(elements: str | Iterable[str]) -> list[str]:
    """Accept newline-separated text or a list; return the cleaned, non-empty elements."""
    if isinstance(elements, str):
        parts = elements.replace("\r", "\n").split("\n")
    else:
        parts = list(elements)
    return [item for item in (clean(part) for part in parts) if item]


def validate_claim(title: str, abstract: str, elements: str | Iterable[str]) -> list[str]:
    """Return a list of problems the contract would reject; an empty list means it fits."""
    problems = []
    raw_elements = elements if isinstance(elements, str) else "\n".join(elements)
    for label, value in (("title", title), ("abstract", abstract), ("elements", raw_elements)):
        if HIDDEN.search(str(value)):
            problems.append(f"{label} contains hidden or control characters")
    cleaned_title, cleaned_abstract = clean(title), clean(abstract)
    if not 1 <= len(cleaned_title) <= MAX_TITLE:
        problems.append(f"title must be 1-{MAX_TITLE} characters")
    if not MIN_ABSTRACT <= len(cleaned_abstract) <= MAX_ABSTRACT:
        problems.append(f"abstract must be {MIN_ABSTRACT}-{MAX_ABSTRACT} characters")
    parts = normalize_elements(elements)
    if not MIN_ELEMENTS <= len(parts) <= MAX_ELEMENTS:
        problems.append(f"need {MIN_ELEMENTS}-{MAX_ELEMENTS} elements")
    if any(not MIN_ELEMENT <= len(part) <= MAX_ELEMENT for part in parts):
        problems.append(f"every element must be {MIN_ELEMENT}-{MAX_ELEMENT} characters")
    if len({part.lower() for part in parts}) != len(parts):
        problems.append("elements must be distinct")
    return problems


def commitment(
    author_address: str,
    title: str,
    abstract: str,
    elements: str | Iterable[str],
    salt: str,
) -> str:
    """Return the sealed-claim commitment exactly as the contract recomputes it."""
    if not SALT_PATTERN.fullmatch(salt):
        raise ValueError("salt must be 16-64 characters from [A-Za-z0-9_.~-]")
    fields = [
        COMMITMENT_DOMAIN,
        author_address.lower(),
        clean(title),
        clean(abstract),
        "\n".join(normalize_elements(elements)),
        salt,
    ]
    data = b""
    for field in fields:
        encoded = field.encode("utf-8")
        data += len(encoded).to_bytes(4, "big") + encoded
    return "0x" + hashlib.sha256(data).hexdigest()


def needed_disclosures(element_count: int, overlap_percent: int) -> int:
    """How many elements must be disclosed for a junior claim to count as derivative."""
    return max(1, (element_count * overlap_percent + 99) // 100)
