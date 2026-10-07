"""Brand chunking (S16 T3/T4; TEAM-QUESTIONS T90; spec [30] L9379-9405): pure, stdlib only.

One chunk per meaningful unit where the unit is known, never a sliding window and never overlap
(R05047b):

- A document (`.txt`/`.md`) splits at blank lines; every bullet (`-`, `*`, `+`, `•`) and numbered
  item (`1.`, `1)`) starts its own unit with the marker stripped; ATX headings, setext headings
  and thematic breaks are dropped. A unit over `MAX_CHUNK_WORDS` words or `MAX_CHUNK_CHARS`
  characters splits at sentence ends (`.`, `!`, `?`, `।`), packed greedily; an over-long sentence
  splits by words and an over-long word by characters.
- The profile: one `tone_rule` per guideline, one `glossary` chunk per pair
  (`Say '<preferred>' not '<avoid>'.`, sorted by term), then the tone split as a document.

Text is NFC-normalised, the BOM and control characters other than `\\n`/`\\t` are removed and
whitespace runs collapse, so the same words always give the same `content_hash` (sha256 hex of the
chunk text). Repeated chunks are dropped. Every regex is anchored and bounded and line work uses
`str.splitlines`/`str.split`, so a hostile 10 MiB body is linear time. `check_chunks` refuses 0 or
too many chunks, any JSON-shaped secret key (the seam walker) and any credential-shaped value
(`CREDENTIAL_VALUE_PATTERNS`); no reason ever quotes the input.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mkos_schemas.base import find_seam_secret_key
from mkos_schemas.jobs import BrandChunkType

MAX_CHUNK_WORDS: Final = 200
MAX_CHUNK_CHARS: Final = 2000
MAX_DOCUMENT_CHUNKS: Final = 500
MAX_PROFILE_CHUNKS: Final = 700
REPEAT_FACTOR: Final = 4

BULLET_MARKERS: Final = frozenset("-*+•")
SENTENCE_ENDS: Final = frozenset(".!?।")
_NUMBERED: Final = re.compile(r"\d{1,9}[.)](?: |$)")
_ATX: Final = re.compile(r"#{1,6}(?: |$)")
# Cc characters except \t and \n, plus the BOM; \r is turned into \n before this runs.
_DROP: Final = dict.fromkeys((*range(0, 9), *range(11, 32), *range(127, 160)))
_DROP[0xFEFF] = None

# Credential shapes (invariant 6): each starts with a literal and has a fixed minimum tail, so
# prose such as `task-based`, `sk-ill` or `Bearer of good news` stays legal and nothing repeats
# without a bound.
CREDENTIAL_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"Atz[ar]\|[A-Za-z0-9_\-]{20}"),
    re.compile(r"amzn1\.oa2-cs\.v1\.[0-9a-f]{20}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"-----BEGIN [A-Z ]{1,40}-----"),
    re.compile(r"\beyJ[\w-]{8,512}\.[\w-]{8,1024}\.[\w-]{8}"),
    re.compile(r"\bBearer [A-Za-z0-9._~+/\-]{16}"),
    re.compile(r"://[^\s/:@]{1,256}:[^\s/@]{1,256}@"),
)


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    index: int
    type: BrandChunkType
    text: str
    content_hash: str


class ChunkingError(Exception):
    """A refused text. `field` is the request path (`file`, `guidelines[3]`), `reason` a fixed
    phrase with indexes only: never the input."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def normalize_text(text: str) -> str:
    """NFC, no BOM, LF line ends, no control characters but `\\n`/`\\t`, and every line's
    whitespace runs collapsed to one space (blank lines stay, they split units)."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_DROP)
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def _squash(text: str) -> str:
    return " ".join(text.split())


def chunk_hash(text: str) -> str:
    return hashlib.sha256(_squash(normalize_text(text)).encode("utf-8")).hexdigest()


def _is_setext_underline(line: str) -> bool:
    return len(line) >= 2 and (set(line) == {"="} or set(line) == {"-"})


def _item_body(line: str) -> str | None:
    """The text of a bullet or numbered item with its marker stripped, or None."""
    if len(line) >= 2 and line[0] in BULLET_MARKERS and line[1] == " ":
        return line[2:]
    if line == "-" or line == "*" or line == "+" or line == "•":
        return ""
    match = _NUMBERED.match(line)
    if match is not None:
        return line[match.end() :]
    return None


def _iter_units(text: str) -> Iterator[str]:
    lines: list[str] = []
    for line in normalize_text(text).splitlines():
        if not line:
            if lines:
                yield " ".join(lines)
                lines = []
            continue
        if _is_setext_underline(line):
            if lines:  # the paragraph's last line is the heading text
                lines.pop()
                if lines:
                    yield " ".join(lines)
                lines = []
            continue
        if _ATX.match(line):
            if lines:
                yield " ".join(lines)
                lines = []
            continue
        body = _item_body(line)
        if body is not None:
            if lines:
                yield " ".join(lines)
            lines = [body] if body else []
            continue
        lines.append(line)
    if lines:
        yield " ".join(lines)


def split_units(text: str) -> list[str]:
    return [unit for unit in (_squash(u) for u in _iter_units(text)) if unit]


def _fits(words: int, chars: int) -> bool:
    return words <= MAX_CHUNK_WORDS and chars <= MAX_CHUNK_CHARS


def _sentences(words: list[str]) -> Iterator[list[str]]:
    current: list[str] = []
    for word in words:
        current.append(word)
        if word[-1] in SENTENCE_ENDS:
            yield current
            current = []
    if current:
        yield current


def _pieces(sentence: list[str]) -> Iterator[list[str]]:
    """A sentence that fits, or its words packed greedily (an over-long word cut by chars)."""
    if _fits(len(sentence), sum(len(w) for w in sentence) + len(sentence) - 1):
        yield sentence
        return
    current: list[str] = []
    chars = 0
    for word in sentence:
        cuts = (
            [word[i : i + MAX_CHUNK_CHARS] for i in range(0, len(word), MAX_CHUNK_CHARS)]
            if len(word) > MAX_CHUNK_CHARS
            else [word]
        )
        for cut in cuts:
            added = len(cut) + (1 if current else 0)
            if current and not _fits(len(current) + 1, chars + added):
                yield current
                current, chars, added = [], 0, len(cut)
            current.append(cut)
            chars += added
    if current:
        yield current


def _iter_capped(unit: str) -> Iterator[str]:
    words = unit.split()
    if _fits(len(words), len(unit)):
        yield unit
        return
    current: list[str] = []
    chars = 0
    for sentence in _sentences(words):
        for piece in _pieces(sentence):
            size = sum(len(w) for w in piece) + len(piece) - 1
            added = size + (1 if current else 0)
            if current and not _fits(len(current) + len(piece), chars + added):
                yield " ".join(current)
                current, chars, added = [], 0, size
            current.extend(piece)
            chars += added
    if current:
        yield " ".join(current)


def cap_unit(unit: str) -> list[str]:
    return list(_iter_capped(_squash(unit)))


def _drafts(
    texts: Iterator[tuple[BrandChunkType, str]], limit: int | None, field: str = "file"
) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    seen: set[str] = set()
    examined = 0
    for chunk_type, text in texts:
        if limit is not None and len(drafts) > limit:
            break
        if not text:
            continue
        examined += 1
        if limit is not None and examined > REPEAT_FACTOR * (limit + 1):
            # mostly repeats (a hostile body): refuse it without walking the rest
            raise ChunkingError(field, f"more than {limit} chunks")
        digest = chunk_hash(text)
        if digest in seen:
            continue
        seen.add(digest)
        drafts.append(ChunkDraft(len(drafts), chunk_type, text, digest))
    return drafts


def chunk_document(
    text: str, chunk_type: BrandChunkType, *, limit: int | None = None, field: str = "file"
) -> list[ChunkDraft]:
    """The document's drafts in order. With `limit`, it stops once draft `limit + 1` exists, so
    `check_chunks(max_chunks=limit)` refuses without the rest being split or hashed; a text whose
    pieces are mostly repeats (more than `REPEAT_FACTOR * (limit + 1)` pieces before that) raises
    `ChunkingError(field, ...)` at once."""

    def texts() -> Iterator[tuple[BrandChunkType, str]]:
        for unit in _iter_units(text):
            for piece in _iter_capped(_squash(unit)):
                yield chunk_type, piece

    return _drafts(texts(), limit, field)


def chunk_profile(
    tone: str | None, guidelines: Sequence[str] | None, glossary: Mapping[str, str] | None
) -> list[ChunkDraft]:
    def texts() -> Iterator[tuple[BrandChunkType, str]]:
        for guideline in guidelines or ():
            for piece in _iter_capped(_squash(normalize_text(guideline))):
                yield "tone_rule", piece
        for avoid in sorted(glossary or {}):
            preferred = _squash(normalize_text((glossary or {})[avoid]))
            yield "glossary", f"Say '{preferred}' not '{_squash(normalize_text(avoid))}'."
        if tone:
            for unit in _iter_units(tone):
                for piece in _iter_capped(_squash(unit)):
                    yield "tone_rule", piece

    return _drafts(texts(), None)


def has_credential_value(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in CREDENTIAL_VALUE_PATTERNS)


def check_chunks(drafts: Sequence[ChunkDraft], *, field: str, max_chunks: int) -> None:
    if not drafts:
        raise ChunkingError(field, "no text to chunk")
    if len(drafts) > max_chunks:
        raise ChunkingError(field, f"more than {max_chunks} chunks")
    for draft in drafts:
        if find_seam_secret_key(draft.text) is not None:
            raise ChunkingError(field, f"chunk {draft.index} contains a forbidden key")
        if has_credential_value(draft.text):
            raise ChunkingError(field, f"chunk {draft.index} contains a credential-shaped value")
