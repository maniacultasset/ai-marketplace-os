"""Text primitives of the Rules Engine: normalisation, measuring, the term tokenizer, slots,
value access and the rule-regex compiler with its backtracking guard (S14a PLAN D3, D6 B).

Everything here is pure and deterministic. Caches are keyed on rule content and input text only.
"""

from __future__ import annotations

import importlib
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Literal

from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import resolve
from mkos_schemas.rules import Condition, check_rule_regex

# The stdlib regex parser (no public API, as in mkos_schemas/rules.py). Python is pinned ==3.13.*.
_sre: Any = importlib.import_module("re._constants")
_sre_parser: Any = importlib.import_module("re._parser")

MatchMode = Literal["word", "substring", "regex"]
SlotKind = Literal["text", "length"]
SlotValue = str | list[str]

NOT_LISTING_ROOTS: Final[tuple[str, ...]] = (
    "facts.",
    "brand.",
    "variants",
    "request",
    "file.header",
    "file.row",
)
"""Rule fields and paths the engine cannot read in v1: post-v1 (``facts.py`` contract MINOR; no
v1 owner) (PLAN D2). A rule field or ``cross_field`` side here gives no item."""

_CACHE_SIZE: Final = 1024


class UnsafeRuleRegexError(ValueError):
    """A rule regex the engine refuses to run (S2 gate or the overlap guard, PLAN D6 B)."""


# ---------------------------------------------------------------------------------------------
# Normalisation and measuring (D3a, D3c)


@lru_cache(maxsize=_CACHE_SIZE * 8)
def normalise(text: str, *, case_sensitive: bool) -> str:
    """NFKC, casefold unless ``case_sensitive``, whitespace runs collapsed, ends stripped."""
    folded = unicodedata.normalize("NFKC", text)
    if not case_sensitive:
        folded = folded.casefold()
    out: list[str] = []
    in_space = False
    for ch in folded:
        if ch.isspace():
            in_space = True
            continue
        if in_space and out:
            out.append(" ")
        in_space = False
        out.append(ch)
    return "".join(out)


def _joined(value: SlotValue) -> str:
    return " ".join(value) if isinstance(value, list) else value


def measure(value: SlotValue, unit: Literal["chars", "bytes", "words"]) -> int:
    """Length of a text (a list is joined by one space): code points or UTF-8 bytes after NFC,
    or whitespace-separated words."""
    text = unicodedata.normalize("NFC", _joined(value))
    if unit == "chars":
        return len(text)
    if unit == "bytes":
        return len(text.encode("utf-8"))
    return len(text.split())


# ---------------------------------------------------------------------------------------------
# The term tokenizer (D3a; KNOWN-ISSUES `match="word"`)


def _is_word_char(ch: str) -> bool:
    return ch == "_" or unicodedata.category(ch)[0] in "LNM"


def _word_hit(text: str, term: str) -> bool:
    """True when ``term`` occurs in ``text`` with no letter, digit, mark or ``_`` on either side.
    Every start position is tried, so a failed occurrence never hides a later one."""
    start = text.find(term)
    size = len(term)
    while start != -1:
        end = start + size
        left_ok = start == 0 or not _is_word_char(text[start - 1])
        right_ok = end == len(text) or not _is_word_char(text[end])
        if left_ok and right_ok:
            return True
        start = text.find(term, start + 1)
    return False


_GRAM: Final = 4


@lru_cache(maxsize=_CACHE_SIZE * 4)
def _grams(text: str, size: int) -> frozenset[str]:
    """Every substring of ``text`` of length ``size``."""
    return frozenset(text[i : i + size] for i in range(len(text) - size + 1))


@dataclass(frozen=True)
class _Matcher:
    """One rule's term list, ready to scan. Literal keys are indexed by their first
    ``_GRAM`` characters, so a text is checked only against the keys whose prefix it holds
    (C-level set intersections instead of one substring search per term)."""

    terms: tuple[str, ...]
    keys: tuple[str, ...]
    match: MatchMode
    case_sensitive: bool
    patterns: tuple[re.Pattern[str], ...]
    index: Mapping[int, Mapping[str, tuple[int, ...]]]

    def _candidates(self, norm: str) -> list[int]:
        found: list[int] = []
        for size, by_prefix in self.index.items():
            for prefix in by_prefix.keys() & _grams(norm, size):
                found.extend(by_prefix[prefix])
        found.sort()
        return found

    def find(self, text: str) -> tuple[str, ...]:
        if self.match == "regex":
            nfkc = unicodedata.normalize("NFKC", text)
            return tuple(
                term
                for term, pattern in zip(self.terms, self.patterns, strict=True)
                if pattern.search(nfkc) is not None
            )
        norm = normalise(text, case_sensitive=self.case_sensitive)
        hits: list[str] = []
        for i in self._candidates(norm):
            key = self.keys[i]
            if key in norm and (self.match == "substring" or _word_hit(norm, key)):
                hits.append(self.terms[i])
        return tuple(hits)


def _prefix_index(keys: Sequence[str]) -> dict[int, dict[str, tuple[int, ...]]]:
    index: dict[int, dict[str, list[int]]] = {}
    for i, key in enumerate(keys):
        if key:  # a term that normalises to "" never matches
            prefix = key[:_GRAM]
            index.setdefault(len(prefix), {}).setdefault(prefix, []).append(i)
    return {size: {p: tuple(ix) for p, ix in group.items()} for size, group in index.items()}


@lru_cache(maxsize=_CACHE_SIZE)
def _matcher(terms: tuple[str, ...], match: MatchMode, case_sensitive: bool) -> _Matcher:
    if match == "regex":
        patterns = tuple(compiled_rule_regex(t, ignore_case=not case_sensitive) for t in terms)
        return _Matcher(terms, terms, match, case_sensitive, patterns, {})
    keys = tuple(normalise(t, case_sensitive=case_sensitive) for t in terms)
    return _Matcher(terms, keys, match, case_sensitive, (), _prefix_index(keys))


@lru_cache(maxsize=_CACHE_SIZE * 8)
def _find_cached(
    text: str, terms: tuple[str, ...], match: MatchMode, case_sensitive: bool
) -> tuple[str, ...]:
    return _matcher(terms, match, case_sensitive).find(text)


def find_terms(
    text: str, terms: Sequence[str], *, match: MatchMode, case_sensitive: bool
) -> tuple[str, ...]:
    """The terms of ``terms`` found in ``text``, in rule order (each at most once)."""
    return _find_cached(text, tuple(terms), match, case_sensitive)


# ---------------------------------------------------------------------------------------------
# Rule regex: the S2 gate plus the overlapping-repeat guard (PLAN D6 option B, AC 15)

_PROBE_SAMPLES: Final = tuple(
    chr(c) for c in (0x5F, 0x967, 0x915, 0x901, 0xA0, 0x3000, 0xFF21, 0x1F600)
)
_BASE_PROBES: Final = tuple(chr(c) for c in range(256)) + _PROBE_SAMPLES
_CATEGORY_CLASS: Final = {
    "CATEGORY_DIGIT": r"\d",
    "CATEGORY_NOT_DIGIT": r"\D",
    "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S",
    "CATEGORY_WORD": r"\w",
    "CATEGORY_NOT_WORD": r"\W",
}
_FLAG_MASK: Final = re.IGNORECASE | re.DOTALL | re.ASCII
_ZERO_WIDTH_OPS: Final = frozenset({_sre.AT})
_ATOM_OPS: Final = frozenset({_sre.LITERAL, _sre.NOT_LITERAL, _sre.ANY, _sre.IN})
_REPEAT_OPS: Final = frozenset({_sre.MAX_REPEAT, _sre.MIN_REPEAT})


@dataclass(frozen=True)
class _CharSet:
    """One single-character atom of a regex, as a one-character pattern plus extra probes."""

    source: str
    flags: int
    probes: tuple[str, ...]


@lru_cache(maxsize=_CACHE_SIZE)
def _charset_pattern(source: str, flags: int) -> re.Pattern[str]:
    return re.compile(source, flags)


def _class_item(op: Any, av: Any) -> tuple[str, tuple[str, ...]]:
    if op is _sre.LITERAL:
        return re.escape(chr(av)), (chr(av),)
    if op is _sre.RANGE:
        lo, hi = av
        return f"{re.escape(chr(lo))}-{re.escape(chr(hi))}", (chr(lo), chr(hi))
    if op is _sre.CATEGORY:
        return _CATEGORY_CLASS.get(av.name, r"\s\S"), ()
    return r"\s\S", ()  # unknown item: match anything (more refusals, never fewer)


def _charset(op: Any, av: Any, flags: int) -> _CharSet:
    if op is _sre.LITERAL:
        return _CharSet(re.escape(chr(av)), flags, (chr(av),))
    if op is _sre.NOT_LITERAL:
        return _CharSet(f"[^{re.escape(chr(av))}]", flags, (chr(av),))
    if op is _sre.ANY:
        return _CharSet(".", flags, ())
    negate = ""
    parts: list[str] = []
    probes: list[str] = []
    for item_op, item_av in av:
        if item_op is _sre.NEGATE:
            negate = "^"
            continue
        part, extra = _class_item(item_op, item_av)
        parts.append(part)
        probes.extend(extra)
    return _CharSet(f"[{negate}{''.join(parts)}]", flags, tuple(probes))


def _overlap(a: _CharSet, b: _CharSet) -> bool:
    pa = _charset_pattern(a.source, a.flags)
    pb = _charset_pattern(b.source, b.flags)
    for probe in (*_BASE_PROBES, *a.probes, *b.probes):
        if pa.fullmatch(probe) is not None and pb.fullmatch(probe) is not None:
            return True
    return False


def _overlaps_any(atoms: frozenset[_CharSet], open_set: frozenset[_CharSet]) -> bool:
    return any(_overlap(a, b) for a in atoms for b in open_set)


def _atoms(sub: Any, flags: int) -> frozenset[_CharSet]:
    """Every single-character atom anywhere below ``sub``."""
    found: set[_CharSet] = set()
    for op, av in sub:
        if op in _ATOM_OPS:
            found.add(_charset(op, av, flags))
        elif op in _REPEAT_OPS:
            found |= _atoms(av[2], flags)
        elif op is _sre.SUBPATTERN:
            found |= _atoms(av[3], _sub_flags(flags, av))
        elif op is _sre.BRANCH:
            for alt in av[1]:
                found |= _atoms(alt, flags)
    return frozenset(found)


def _sub_flags(flags: int, av: Any) -> int:
    add_flags: int = av[1]
    del_flags: int = av[2]
    return ((flags | add_flags) & ~del_flags) & _FLAG_MASK


class _RefusedError(Exception):
    pass


def _walk(sub: Any, open_set: frozenset[_CharSet], flags: int) -> frozenset[_CharSet]:
    """Walk one sequence left to right, returning the open unbounded repeats after it."""
    for op, av in sub:
        if op in _ZERO_WIDTH_OPS:
            continue
        if op in _ATOM_OPS:
            atom = _charset(op, av, flags)
            if not _overlaps_any(frozenset({atom}), open_set):
                open_set = frozenset()
            continue
        if op in _REPEAT_OPS:
            low, high, body = av
            if high == _sre.MAXREPEAT:
                atoms = _atoms(body, flags)
                if _overlaps_any(atoms, open_set):
                    raise _RefusedError
                open_set = open_set | atoms if low == 0 else atoms
            else:
                after = _walk(body, open_set, flags)
                open_set = open_set | after if low == 0 else after
            continue
        if op is _sre.SUBPATTERN:
            open_set = _walk(av[3], open_set, _sub_flags(flags, av))
            continue
        if op is _sre.BRANCH:
            merged: frozenset[_CharSet] = frozenset()
            for alt in av[1]:
                merged |= _walk(alt, open_set, flags)
            open_set = merged
            continue
        # Any other node (the S2 gate refuses lookaround and backreferences): it matches
        # something unknown, so it closes nothing.
    return open_set


def _overlapping_repeat(tree: Any, *, ignore_case: bool = False) -> str | None:
    """A reason when two unbounded repeats can match the same character with no disjoint
    mandatory atom between them (``\\s*\\s*x``, ``.*foo.*``); else ``None``."""
    flags = tree.state.flags & _FLAG_MASK
    if ignore_case:
        flags |= re.IGNORECASE
    try:
        _walk(tree, frozenset(), flags)
    except _RefusedError:
        return "adjacent overlapping unbounded repeats can backtrack polynomially"
    return None


def compile_rule_regex(regex: str, *, ignore_case: bool) -> re.Pattern[str]:
    """Compile a rule regex after the S2 contract gate and the overlap guard; refuse with
    ``UnsafeRuleRegexError``. The single place to swap in RE2 (PLAN D6 option A)."""
    try:
        check_rule_regex(regex)
        tree = _sre_parser.parse(regex)
    except (ValueError, re.error, OverflowError, RecursionError) as exc:
        raise UnsafeRuleRegexError(str(exc)) from None
    reason = _overlapping_repeat(tree, ignore_case=ignore_case)
    if reason is not None:
        raise UnsafeRuleRegexError(reason)
    return re.compile(regex, re.IGNORECASE if ignore_case else 0)


@lru_cache(maxsize=_CACHE_SIZE)
def compiled_rule_regex(regex: str, *, ignore_case: bool) -> re.Pattern[str]:
    """Memoised ``compile_rule_regex`` (a refusal is not cached and raises every time)."""
    return compile_rule_regex(regex, ignore_case=ignore_case)


# ---------------------------------------------------------------------------------------------
# Slots and value access (D3b, D10h field matrix)


def is_not_listing_root(path: str) -> bool:
    return path.startswith(NOT_LISTING_ROOTS)


_ATTR_PREFIX: Final = "attributes."
_INDEXED = re.compile(
    r"^(content\.bullets|keywords\.search_terms|keywords\.hidden_terms)\[(\d+)\]$"
)


def _star_slots(listing: CanonicalListing) -> list[tuple[str, SlotValue]]:
    slots: list[tuple[str, SlotValue]] = [("content.title", listing.content.title)]
    slots += [(f"content.bullets[{i}]", b) for i, b in enumerate(listing.content.bullets)]
    slots.append(("content.description", listing.content.description))
    slots += [
        (f"keywords.search_terms[{i}]", t) for i, t in enumerate(listing.keywords.search_terms)
    ]
    slots += [
        (f"keywords.hidden_terms[{i}]", t) for i, t in enumerate(listing.keywords.hidden_terms)
    ]
    return slots


def _list_field(listing: CanonicalListing, name: str) -> list[str]:
    if name == "content.bullets":
        return listing.content.bullets
    if name == "keywords.search_terms":
        return listing.keywords.search_terms
    return listing.keywords.hidden_terms


def iter_slots(
    listing: CanonicalListing, field: str, *, kind: SlotKind
) -> list[tuple[str, SlotValue]]:
    """The (concrete path, value) pairs a text or length rule on ``field`` reads, in canonical
    order (CODE-PLAN T3 field matrix). Unreadable fields give no slot."""
    if field == "*":
        return _star_slots(listing)
    if field == "content.title":
        return [(field, listing.content.title)]
    if field == "content.description":
        return [(field, listing.content.description)]
    if field == "content.bullets[*]":
        return [(f"content.bullets[{i}]", b) for i, b in enumerate(listing.content.bullets)]
    if field in ("content.bullets", "keywords.search_terms", "keywords.hidden_terms"):
        items = _list_field(listing, field)
        if kind == "length":
            return [(field, list(items))]
        return [(f"{field}[{i}]", item) for i, item in enumerate(items)]
    indexed = _INDEXED.match(field)
    if indexed is not None:
        items = _list_field(listing, indexed.group(1))
        index = int(indexed.group(2))
        return [(field, items[index])] if index < len(items) else []
    if field.startswith(_ATTR_PREFIX):
        value = listing.attributes.get(field[len(_ATTR_PREFIX) :])
        if isinstance(value, str):
            return [(field, value)]
        if isinstance(value, list):
            if kind == "length":
                return [(field, list(value))]
            return [(field, item) for item in value]
        return []  # absent, number or bool
    return []  # attributes, images, listing and NOT_LISTING_ROOTS


def _mapping_get(mapping: Mapping[str, object] | None, parts: Sequence[str]) -> tuple[bool, object]:
    node: object = mapping
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def lookup(
    listing: CanonicalListing,
    path: str,
    *,
    product: Mapping[str, object] | None,
    account: Mapping[str, object] | None,
) -> tuple[bool, object]:
    """(found, value) for a field or condition path. Never raises."""
    if path.startswith("product."):
        return _mapping_get(product, [path[len("product.") :]])
    if path.startswith("account."):
        return _mapping_get(account, path[len("account.") :].split("."))
    if path == "listing" or is_not_listing_root(path):
        return False, None
    try:
        return True, resolve(listing, path)
    except (KeyError, IndexError, ValueError, TypeError):
        return False, None


def is_present(value: object) -> bool:
    """False for ``None``, blank text and an empty list (or a list of blank items); true for
    ``False`` and ``0``."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, list):
        return any(is_present(item) for item in value)
    return True


def fold(value: str) -> str:
    """The ``casefold_trim`` comparison form."""
    return value.strip().casefold()


def scalar_equal(a: object, b: object) -> bool:
    """D10b equality: strings after casefold+strip; ``bool`` never equals a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, str) and isinstance(b, str):
        return fold(a) == fold(b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return False


def _equals(value: object, target: object) -> bool:
    if isinstance(value, list):
        return any(scalar_equal(item, target) for item in value)
    return scalar_equal(value, target)


def condition_holds(
    cond: Condition,
    listing: CanonicalListing,
    *,
    product: Mapping[str, object] | None,
    account: Mapping[str, object] | None,
) -> bool:
    """D10b: ``present``/``absent`` test presence; every other op on an absent value is false."""
    found, value = lookup(listing, cond.path, product=product, account=account)
    if cond.op == "present":
        return found and is_present(value)
    if cond.op == "absent":
        return not (found and is_present(value))
    if not found or value is None:
        return False
    checks: dict[str, Callable[[], bool]] = {
        "eq": lambda: _equals(value, cond.value),
        "ne": lambda: not _equals(value, cond.value),
        "in": lambda: any(_equals(value, v) for v in cond.values or ()),
        "not_in": lambda: not any(_equals(value, v) for v in cond.values or ()),
    }
    return checks[cond.op]()
