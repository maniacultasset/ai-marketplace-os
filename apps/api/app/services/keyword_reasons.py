"""Why these keywords, kept true to the copy (client feedback item 11, bullet 6; schemas 2.1.0).

`CanonicalListing.keyword_reasons` is written by the pipeline (copy v5, `mkos-pipeline` 0.8.2): per
search term a reason, a source, the product paths it repeats and where the copy uses it. Anything
that changes a listing after the pipeline (a section regenerate's merge, the locked paths put back,
the fix pass, a seller edit) goes through `refresh_keyword_reasons`, so:

- only the terms still listed keep a reason (a term the seller added has none);
- a reason comes from the first source listing that has one for the term;
- `used_in` is recomputed on the listing's own copy, with the pipeline's matcher
  (`mkos_pipeline.checks.copy.keyword_covered`): every content word of the term, stemmed, in any
  order, a short-prefix compound also joined ("T-Shirt" covers "tshirt"). `backend` is always
  there, since every listed term goes to the marketplace's search-term field.

Pure (no I/O). `fact_keys` and `source` are the pipeline's and are kept as written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from mkos_schemas.listing import MAX_KEYWORD_REASONS, CanonicalListing, KeywordReason

_STOPWORDS: Final = frozenset(
    {"a", "an", "and", "the", "for", "with", "of", "in", "on", "to", "by", "or"}
)
_WORD: Final = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_COMPOUND: Final = re.compile(r"\b([a-z0-9]{1,3})[\s\-]([a-z]{3,})\b")


def _stem(word: str) -> str:
    word = word.removesuffix("'s")
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [_stem(word) for word in _WORD.findall(text.casefold())]


def _coverage_words(text: str) -> set[str]:
    folded = text.casefold()
    joined = {_stem(a + b) for a, b in _COMPOUND.findall(folded)}
    return set(_words(folded)) | joined


def keyword_covered(term: str, text: str) -> bool:
    """The pipeline's `keyword_covered`, byte for byte (module docstring)."""
    needed = {word for word in _words(term) if word not in _STOPWORDS}
    return bool(needed) and needed <= _coverage_words(text)


def used_in(term: str, listing: CanonicalListing) -> list[str]:
    content = listing.content
    places: list[str] = []
    if keyword_covered(term, content.title):
        places.append("title")
    if any(keyword_covered(term, bullet) for bullet in content.bullets):
        places.append("bullets")
    if keyword_covered(term, content.description):
        places.append("description")
    places.append("backend")
    return places


def refresh_keyword_reasons(
    listing: CanonicalListing, sources: Iterable[CanonicalListing] = ()
) -> CanonicalListing:
    """`listing` with its reasons limited to its own terms, taken from `listing` itself and then
    each of `sources` in order, and `used_in` recomputed on its copy (module docstring)."""
    found: dict[str, KeywordReason] = {}
    for source in (listing, *sources):
        for reason in source.keyword_reasons:
            found.setdefault(reason.term, reason)
    terms = dict.fromkeys([*listing.keywords.search_terms, *listing.keywords.hidden_terms])
    reasons = [
        found[term].model_copy(update={"used_in": used_in(term, listing)})
        for term in terms
        if term in found
    ][:MAX_KEYWORD_REASONS]
    if reasons == listing.keyword_reasons:
        return listing
    return listing.model_copy(update={"keyword_reasons": reasons})
