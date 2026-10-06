"""Text primitives of the Rules Engine: normalisation, measuring, the term tokenizer, slots,
value access and the rule-regex compiler with its backtracking guard (S14a PLAN D3, D6 B).

Everything here is pure and deterministic. Process-wide caches are keyed on rule content only
(matchers, compiled regexes); work on listing text is memoised per ``evaluate`` call in a
``TextMemo``, so a long-running worker never holds listing text (fix round 1, R1-3).
"""

from __future__ import annotations

import importlib
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Any, Final, Literal

from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import resolve
from mkos_schemas.rules import Condition, check_rule_regex

# The stdlib regex parser (no public API, as in mkos_schemas/rules.py). Python is pinned ==3.13.*.
_sre: Any = importlib.import_module("re._constants")
_sre_parser: Any = importlib.import_module("re._parser")
_sre_lib: Any = importlib.import_module("_sre")
_sre_casefix: Any = importlib.import_module("re._casefix")

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


def normalise(text: str, *, case_sensitive: bool) -> str:
    """NFKC, casefold unless ``case_sensitive``, whitespace runs collapsed, ends stripped."""
    folded = unicodedata.normalize("NFKC", text)
    if not case_sensitive:
        folded = folded.casefold()
    return " ".join(folded.split())  # split() breaks on str.isspace runs and drops the ends


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


def _grams(text: str, size: int) -> frozenset[str]:
    """Every substring of ``text`` of length ``size``."""
    return frozenset(text[i : i + size] for i in range(len(text) - size + 1))


@dataclass
class TextMemo:
    """Scratch memo for one ``evaluate`` call: normalised texts, their n-grams and term hits.
    It lives only as long as the call's ``Ctx``, so its size is bounded by one listing."""

    norms: dict[tuple[str, bool], str] = field(default_factory=dict)
    gram_sets: dict[tuple[str, int], frozenset[str]] = field(default_factory=dict)
    hits: dict[tuple[str, tuple[str, ...], MatchMode, bool], tuple[str, ...]] = field(
        default_factory=dict
    )

    def normalise(self, text: str, *, case_sensitive: bool) -> str:
        key = (text, case_sensitive)
        if key not in self.norms:
            self.norms[key] = normalise(text, case_sensitive=case_sensitive)
        return self.norms[key]

    def grams(self, text: str, size: int) -> frozenset[str]:
        key = (text, size)
        if key not in self.gram_sets:
            self.gram_sets[key] = _grams(text, size)
        return self.gram_sets[key]


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

    def _candidates(self, norm: str, memo: TextMemo) -> list[int]:
        found: list[int] = []
        for size, by_prefix in self.index.items():
            for prefix in by_prefix.keys() & memo.grams(norm, size):
                found.extend(by_prefix[prefix])
        found.sort()
        return found

    def find(self, text: str, memo: TextMemo) -> tuple[str, ...]:
        if self.match == "regex":
            nfkc = unicodedata.normalize("NFKC", text)
            return tuple(
                term
                for term, pattern in zip(self.terms, self.patterns, strict=True)
                if pattern.search(nfkc) is not None
            )
        norm = memo.normalise(text, case_sensitive=self.case_sensitive)
        hits: list[str] = []
        for i in self._candidates(norm, memo):
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


def find_terms(
    text: str,
    terms: Sequence[str],
    *,
    match: MatchMode,
    case_sensitive: bool,
    memo: TextMemo | None = None,
) -> tuple[str, ...]:
    """The terms of ``terms`` found in ``text``, in rule order (each at most once). ``memo``
    shares work on the same text across the rules of one ``evaluate`` call."""
    memo = TextMemo() if memo is None else memo
    key = (text, tuple(terms), match, case_sensitive)
    if key not in memo.hits:
        memo.hits[key] = _matcher(key[1], match, case_sensitive).find(text, memo)
    return memo.hits[key]


# ---------------------------------------------------------------------------------------------
# Rule regex: the S2 gate plus the overlapping-repeat guard (PLAN D6 option B, AC 15)

_MAX_CODE_POINT: Final = 0x10FFFF
_PROBE_SAMPLES: Final = tuple(
    chr(c) for c in (0x5F, 0x967, 0x915, 0x901, 0xA0, 0x3000, 0xFF21, 0x1F600, _MAX_CODE_POINT)
)
_BASE_PROBE_TEXT: Final = "".join(map(chr, range(256))) + "".join(_PROBE_SAMPLES)
_CATEGORY_RUNS: Final = (r"\w+", r"\d+", r"\s+")


def _around(points: set[int]) -> str:
    """``points`` and their neighbours as one probe text, in code-point order."""
    near = {q for p in points for q in (p - 1, p, p + 1) if 0 <= q <= _MAX_CODE_POINT}
    return "".join(chr(c) for c in sorted(near))


def _category_probe_text() -> str:
    """Both ends of every maximal run of the Unicode ``\\w``, ``\\d`` and ``\\s`` (their
    negations share the ends; IGNORECASE does not change them) and their neighbours (R3-1)."""
    space = "".join(map(chr, range(_MAX_CODE_POINT + 1)))
    ends: set[int] = set()
    for run in _CATEGORY_RUNS:
        for m in re.finditer(run, space):
            ends.update((m.start(), m.end()))
    return _around(ends)


def _cased_points() -> frozenset[int]:
    """Every code point IGNORECASE can relate to another: cased characters (lower or upper case
    differs), every lowercase they map to, and the regex engine's extra case pairs. Outside
    this set a character's lower and upper case are itself and no other character folds to it,
    so a literal or range matches it under IGNORECASE exactly when it does without it."""
    space = range(_MAX_CODE_POINT + 1)
    cased = set(filter(_sre_lib.unicode_iscased, space))
    cased.update(map(_sre_lib.unicode_tolower, cased))
    for key, pairs in _sre_casefix._EXTRA_CASES.items():
        cased.add(key)
        cased.update(pairs)
    return frozenset(cased)


_CASED: Final = _cased_points()


def _fold_probe_text() -> str:
    """Every character IGNORECASE can relate to another and their neighbours. Outside this set
    a class under IGNORECASE matches exactly what it matches without it, so its interval ends
    stay probed (R3-1)."""
    return _around(set(_CASED))


_CATEGORY_PROBE_TEXT: Final = _category_probe_text()
_FOLD_PROBE_TEXT: Final = _fold_probe_text()


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
    """One single-character atom of a regex, as a one-character pattern plus extra probes:
    the ends of its own ranges and literals and their neighbours (R3-1). ``items`` are its class
    items (``None`` for ``.``) and ``negate`` its negation, from which its masks over the shared
    probe tiers are built without scanning them (see ``_tier_mask``)."""

    source: str
    flags: int
    probes: tuple[str, ...]
    has_category: bool = False
    items: tuple[tuple[Any, Any], ...] | None = field(default=(), compare=False)
    negate: bool = field(default=False, compare=False)


@lru_cache(maxsize=_CACHE_SIZE)
def _charset_pattern(source: str, flags: int) -> re.Pattern[str]:
    return re.compile(source, flags)


def _near(*points: int) -> tuple[str, ...]:
    """The points and their neighbours: every interval end of a set built from them."""
    return tuple(chr(q) for p in points for q in (p - 1, p, p + 1) if 0 <= q <= _MAX_CODE_POINT)


def _item_source(op: Any, av: Any) -> str:
    if op is _sre.LITERAL:
        return re.escape(chr(av))
    if op is _sre.RANGE:
        lo, hi = av
        return f"{re.escape(chr(lo))}-{re.escape(chr(hi))}"
    if op is _sre.CATEGORY:
        return _CATEGORY_CLASS.get(av.name, r"\s\S")
    return r"\s\S"  # unknown item: match anything (more refusals, never fewer)


def _class_item(op: Any, av: Any) -> tuple[str, tuple[str, ...]]:
    if op is _sre.LITERAL:
        return _item_source(op, av), _near(av)
    if op is _sre.RANGE:
        return _item_source(op, av), _near(*av)
    return _item_source(op, av), ()


def _charset(op: Any, av: Any, flags: int) -> _CharSet:
    if op is _sre.LITERAL:
        return _CharSet(re.escape(chr(av)), flags, _near(av), items=((op, av),))
    if op is _sre.NOT_LITERAL:
        literal = ((_sre.LITERAL, av),)
        return _CharSet(f"[^{re.escape(chr(av))}]", flags, _near(av), items=literal, negate=True)
    if op is _sre.ANY:
        return _CharSet(".", flags, (), items=None)
    negate = False
    parts: list[str] = []
    probes: list[str] = []
    items: list[tuple[Any, Any]] = []
    has_category = False
    for item_op, item_av in av:
        if item_op is _sre.NEGATE:
            negate = True
            continue
        has_category = has_category or item_op is _sre.CATEGORY
        part, extra = _class_item(item_op, item_av)
        parts.append(part)
        probes.extend(extra)
        items.append((item_op, item_av))
    source = f"[{'^' if negate else ''}{''.join(parts)}]"
    return _CharSet(source, flags, tuple(probes), has_category, tuple(items), negate)


_GUARD_STEP_BUDGET: Final = 20_000
"""Guard work units (nodes walked plus atom pairs compared) per regex; above it the guard
refuses (fail closed). A 256-char regex that is not hostile stays far below it (R2-1, R2-2)."""
_MAX_OPEN_ATOMS: Final = 64
"""Distinct atoms an open set may hold; above it the guard refuses (fail closed, R2-2)."""


def _scan_mask(source: str, flags: int, text: str) -> int:
    """Bit ``i`` set when the one-character pattern matches ``text[i]``: one ``finditer``."""
    mask = 0
    for m in _charset_pattern(source, flags).finditer(text):
        mask |= 1 << m.start()
    return mask


@dataclass(frozen=True, eq=False)
class _Tier:
    """A probe set in bit order: its uncased probes (sorted, bits from 0), then its cased ones
    (sorted, after them). A literal or range matches an uncased probe exactly when the probe
    lies inside it, with or without IGNORECASE (``_cased_points``), so those bits are one
    contiguous run found by bisection; only the cased part is ever scanned, and only under
    IGNORECASE for an item that is cased itself or a range."""

    text: str
    plain: tuple[int, ...]
    cased: tuple[int, ...]
    cased_text: str
    full: int

    @classmethod
    def of(cls, probes: str) -> _Tier:
        points = sorted(set(map(ord, probes)))
        plain = tuple(c for c in points if c not in _CASED)
        cased = tuple(c for c in points if c in _CASED)
        cased_text = "".join(map(chr, cased))
        text = "".join(map(chr, plain)) + cased_text
        return cls(text, plain, cased, cased_text, (1 << len(text)) - 1)


def _span(points: tuple[int, ...], lo: int, hi: int) -> int:
    """Bits of the sorted ``points`` inside ``[lo, hi]``: one contiguous run."""
    return ((1 << bisect_right(points, hi)) - 1) ^ ((1 << bisect_left(points, lo)) - 1)


def _item_bits(op: Any, av: Any, flags: int, tier: _Tier) -> int:
    """One class item's mask over a tier. A category item is scanned whole (few distinct, so
    cached in ``_item_mask``); a literal or range takes its uncased bits by bisection, and its
    cased bits by bisection, or under IGNORECASE by scanning the cased part (an uncased literal
    matches only itself there, so it skips the scan). Exact: a class matches a character when
    one of its items does, and IGNORECASE tests the lowered character, whose ``\\w``/``\\d``/
    ``\\s`` class is its own (``test_regex_guard_cost_probes.py``)."""
    if op is _sre.LITERAL:
        lo = hi = av
    elif op is _sre.RANGE:
        lo, hi = av
    else:
        return _scan_mask(f"[{_item_source(op, av)}]", flags, tier.text)
    if not flags & re.IGNORECASE or (lo == hi and lo not in _CASED):
        cased = _span(tier.cased, lo, hi)
    else:
        cased = _scan_mask(f"[{_item_source(op, av)}]", flags, tier.cased_text)
    return _span(tier.plain, lo, hi) | cased << len(tier.plain)


def _atom_bits(atom: _CharSet, tier: _Tier, item: Callable[[Any, Any], int]) -> int:
    """The atom's mask over a tier: the union of its items' masks (``item``), complemented when
    the atom is negated; ``.`` is scanned (few distinct flag sets)."""
    if atom.items is None:
        return _scan_mask(atom.source, atom.flags, tier.text)
    mask = 0
    for op, av in atom.items:
        mask |= item(op, av)
    return tier.full ^ mask if atom.negate else mask


# The shared probe tiers: 0 base, 1 category run ends, 2 case folds. Built at import (about
# 0.2 s, once per process), so no evaluate call pays for it and the guard's latency stays flat
# from the first rule regex on.
_TIERS: Final = (
    _Tier.of(_BASE_PROBE_TEXT),
    _Tier.of(_CATEGORY_PROBE_TEXT),
    _Tier.of(_FOLD_PROBE_TEXT),
)


@lru_cache(maxsize=_CACHE_SIZE)
def _item_mask(op: Any, av: Any, flags: int, tier: int) -> int:
    return _item_bits(op, av, flags, _TIERS[tier])


@lru_cache(maxsize=_CACHE_SIZE)
def _tier_mask(atom: _CharSet, tier: int) -> int:
    """The atom's mask over one shared tier, built from its items, so an atom costs a few
    bisections and short scans however many probes the tier holds (security review, 402d888)."""
    return _atom_bits(atom, _TIERS[tier], lambda op, av: _item_mask(op, av, atom.flags, tier))


def _sub_flags(flags: int, av: Any) -> int:
    add_flags: int = av[1]
    del_flags: int = av[2]
    scoped = (flags | add_flags) & ~del_flags
    if add_flags & re.UNICODE:  # (?u:...) under a global (?a) restores Unicode classes
        scoped &= ~re.ASCII
    return scoped & _FLAG_MASK


class _RefusedError(Exception):
    def __init__(
        self, reason: str = "adjacent overlapping variable-width repeats can backtrack polynomially"
    ) -> None:
        super().__init__(reason)
        self.reason = reason


_TOO_COMPLEX: Final = "regex too complex for the backtracking guard (fail closed)"


_WalkKey = tuple[int, frozenset[_CharSet], int]


class _Guard:
    """One guard run over one parsed regex: memoised walks and atom sets (keyed on the node's
    ``id``, valid while the tree is alive) and a fail-closed step budget, so the guard is linear
    in the regex for nested optional groups (R2-1) and bounded for wide open sets (R2-2)."""

    def __init__(self, tree: Any, flags: int) -> None:
        self.steps = 0
        self._walks: dict[_WalkKey, frozenset[_CharSet]] = {}
        self._atom_sets: dict[tuple[int, int], frozenset[_CharSet]] = {}
        self._masks: dict[_CharSet, int] = {}
        every = self.atoms(tree, flags)
        self._extra = _Tier.of("".join({p for atom in every for p in atom.probes}))
        # (tier, bit offset): the tiers this regex needs, each at its own bits.
        self._tiers: list[tuple[int, int]] = [(0, 0)]
        offset = len(_TIERS[0].text)
        if any(a.has_category and not a.flags & re.ASCII for a in every):
            self._tiers.append((1, offset))
            offset += len(_TIERS[1].text)
        if any(a.flags & re.IGNORECASE and not a.flags & re.ASCII for a in every):
            self._tiers.append((2, offset))
            offset += len(_TIERS[2].text)
        self._extra_offset = offset
        self._extra_items: dict[tuple[Any, Any, int], int] = {}

    def _extra_item(self, op: Any, av: Any, flags: int) -> int:
        """An item's mask over this regex's own probes, memoised for the guard run."""
        key = (op, av, flags)
        cached = self._extra_items.get(key)
        if cached is None:
            cached = self._extra_items[key] = _item_bits(op, av, flags, self._extra)
        return cached

    def mask(self, atom: _CharSet) -> int:
        """The probes the atom matches. Each atom's set is a union of code-point intervals, and
        two such sets meet exactly when one holds a left end of an interval of the other; the
        probes hold every such end (own ranges and literals with neighbours, Unicode category
        run ends, case-fold partners under IGNORECASE), so masks meet exactly when the atoms
        overlap (R3-1)."""
        cached = self._masks.get(atom)
        if cached is None:
            cached = 0
            for tier, offset in self._tiers:
                cached |= _tier_mask(atom, tier) << offset
            item = partial(self._extra_item, flags=atom.flags)
            cached |= _atom_bits(atom, self._extra, item) << self._extra_offset
            self._masks[atom] = cached
        return cached

    def _charge(self, units: int) -> None:
        self.steps += units
        if self.steps > _GUARD_STEP_BUDGET:
            raise _RefusedError(_TOO_COMPLEX)

    def overlaps_any(self, atoms: frozenset[_CharSet], open_set: frozenset[_CharSet]) -> bool:
        if not atoms or not open_set:
            return False
        if len(open_set) > _MAX_OPEN_ATOMS:
            raise _RefusedError(_TOO_COMPLEX)
        self._charge(len(atoms) + len(open_set))
        open_mask = 0
        for b in open_set:
            b_mask = self.mask(b)
            if not b_mask:  # matches no probe: fail closed, as if it overlapped everything
                return True
            open_mask |= b_mask
        return any(not m or m & open_mask for m in map(self.mask, atoms))

    def atoms(self, sub: Any, flags: int) -> frozenset[_CharSet]:
        """Every single-character atom anywhere below ``sub``."""
        key = (id(sub), flags)
        cached = self._atom_sets.get(key)
        if cached is not None:
            return cached
        self._charge(1)
        found: set[_CharSet] = set()
        for op, av in sub:
            if op in _ATOM_OPS:
                found.add(_charset(op, av, flags))
            elif op in _REPEAT_OPS:
                found |= self.atoms(av[2], flags)
            elif op is _sre.SUBPATTERN:
                found |= self.atoms(av[3], _sub_flags(flags, av))
            elif op is _sre.BRANCH:
                for alt in av[1]:
                    found |= self.atoms(alt, flags)
        result = frozenset(found)
        self._atom_sets[key] = result
        return result

    def walk_optional(
        self, body: Any, open_set: frozenset[_CharSet], flags: int
    ) -> frozenset[_CharSet]:
        """The open set after an optional body; raises ``_RefusedError`` when the body can
        consume characters an open repeat could also take (an open repeat survives it unclosed).
        Both walks are memoised, so nested optional groups stay linear (R2-1)."""
        after = self.walk(body, open_set, flags)
        atoms = self.atoms(body, flags)
        if self.overlaps_any(atoms, after - self.walk(body, frozenset(), flags)):
            raise _RefusedError
        return open_set | after | atoms

    def walk(self, sub: Any, open_set: frozenset[_CharSet], flags: int) -> frozenset[_CharSet]:
        """Walk one sequence left to right, returning the open variable-width repeats after it.

        A repeat with a variable count above one (``*``, ``+``, ``{n,}`` and ``{n,m}`` with
        m > n) is open, bounded or not: stacked overlapping bounded repeats backtrack too
        (``\\d{0,8}`` six times, ``\\d{0,1000}`` three times). An optional body (``?``) is
        walked, refused when no mandatory atom of it closes an open repeat it overlaps
        (``\\d*\\d?``), and leaves its atoms open (``\\d?`` twenty times). A fixed count
        ``{n}`` is walked as its body."""
        key = (id(sub), open_set, flags)
        cached = self._walks.get(key)
        if cached is not None:
            return cached
        self._charge(1)
        start = open_set
        for op, av in sub:
            self._charge(1)
            if op in _ZERO_WIDTH_OPS:
                continue
            if op in _ATOM_OPS:
                atom = _charset(op, av, flags)
                if not self.overlaps_any(frozenset({atom}), open_set):
                    open_set = frozenset()
                continue
            if op in _REPEAT_OPS:
                low, high, body = av
                if high > max(low, 1):  # a variable count above one, bounded or not
                    atoms = self.atoms(body, flags)
                    if self.overlaps_any(atoms, open_set):
                        raise _RefusedError
                    open_set = open_set | atoms if low == 0 else atoms
                elif high > low:  # optional (``?``, ``{0,1}``)
                    open_set = self.walk_optional(body, open_set, flags)
                else:  # a fixed count ``{n}``
                    open_set = self.walk(body, open_set, flags)
                continue
            if op is _sre.SUBPATTERN:
                open_set = self.walk(av[3], open_set, _sub_flags(flags, av))
                continue
            if op is _sre.BRANCH:
                merged: frozenset[_CharSet] = frozenset()
                for alt in av[1]:
                    merged |= self.walk(alt, open_set, flags)
                open_set = merged
                continue
            # Any other node (the S2 gate refuses lookaround and backreferences): it matches
            # something unknown, so it closes nothing.
        if len(open_set) > _MAX_OPEN_ATOMS:
            raise _RefusedError(_TOO_COMPLEX)
        self._walks[(id(sub), start, flags)] = open_set
        return open_set


def _overlapping_repeat(tree: Any, *, ignore_case: bool = False) -> str | None:
    """A reason when two variable-width repeats can match the same character with no disjoint
    mandatory atom between them (``\\s*\\s*x``, ``.*foo.*``, ``\\d{0,99}\\d{0,99}x``), or when
    the guard's own work exceeds its budget (fail closed); else ``None``."""
    flags = tree.state.flags & _FLAG_MASK
    if ignore_case:
        flags |= re.IGNORECASE
    try:
        _Guard(tree, flags).walk(tree, frozenset(), flags)
    except _RefusedError as exc:
        return exc.reason
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
def _compiled_or_refusal(regex: str, ignore_case: bool) -> re.Pattern[str] | str:
    try:
        return compile_rule_regex(regex, ignore_case=ignore_case)
    except UnsafeRuleRegexError as exc:
        return str(exc)


def compiled_rule_regex(regex: str, *, ignore_case: bool) -> re.Pattern[str]:
    """Memoised ``compile_rule_regex``. A refusal is memoised too (its reason), so a refused
    rule regex costs the guard once per process, then raises from the cache (R2-2)."""
    result = _compiled_or_refusal(regex, ignore_case)
    if isinstance(result, str):
        raise UnsafeRuleRegexError(result)
    return result


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
