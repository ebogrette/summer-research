"""Client-side keyword matching.

Every adapter runs text through a `Matcher` so the API can report *which* keywords
hit and in which field — even reddit/twitter, where the platform did the filtering
natively, need this to populate `matched_keywords`.

Design (PLAN §7):
- precompile keywords into a single regex with word boundaries
- support `any` / `all` / `phrase`
- case folding and Unicode (NFKC) normalization
- return which keywords matched, not just a boolean
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def _normalize(text: str) -> str:
    """NFKC-normalize so visually equivalent characters compare equal."""
    return unicodedata.normalize("NFKC", text)


def _boundary_pattern(keyword: str) -> str:
    """Regex source matching `keyword` on word boundaries.

    `\\b` only works between a word char and a non-word char, so for keywords that
    start/end with non-word characters (e.g. "c++", "$gme") we fall back to
    lookarounds that just require the neighbour not be a word char.
    """
    esc = re.escape(keyword)
    left = r"\b" if keyword[:1].isalnum() or keyword[:1] == "_" else r"(?<!\w)"
    right = r"\b" if keyword[-1:].isalnum() or keyword[-1:] == "_" else r"(?!\w)"
    return f"{left}{esc}{right}"


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    keywords: list[str]  # distinct keywords that hit, in the order given


class Matcher:
    """Precompiled matcher for one set of keywords under one mode.

    Reuse a `Matcher` across many texts — the compilation is the expensive part.
    """

    def __init__(
        self,
        keywords: list[str],
        *,
        mode: str = "any",
        case_sensitive: bool = False,
    ) -> None:
        if mode not in ("any", "all", "phrase"):
            raise ValueError(f"unknown match mode: {mode!r}")
        # Drop blanks and de-dupe while preserving caller order.
        seen: set[str] = set()
        cleaned: list[str] = []
        for kw in keywords:
            kw = kw.strip()
            if kw and kw not in seen:
                seen.add(kw)
                cleaned.append(kw)
        if not cleaned:
            raise ValueError("at least one non-empty keyword is required")

        self.keywords = cleaned
        self.mode = mode
        self.case_sensitive = case_sensitive
        self._flags = re.UNICODE | (0 if case_sensitive else re.IGNORECASE)

        if mode == "phrase":
            # The whole keyword list is a single phrase; join on flexible whitespace.
            phrase = r"\s+".join(re.escape(_normalize(kw)) for kw in cleaned)
            left = r"\b" if cleaned[0][:1].isalnum() else r"(?<!\w)"
            right = r"\b" if cleaned[-1][-1:].isalnum() else r"(?!\w)"
            self._phrase_re = re.compile(f"{left}{phrase}{right}", self._flags)
            self._per_keyword = None
        else:
            self._phrase_re = None
            self._per_keyword = [
                re.compile(_boundary_pattern(_normalize(kw)), self._flags)
                for kw in cleaned
            ]

    def search(self, text: str | None) -> MatchResult:
        """Match against one text blob."""
        if not text:
            return MatchResult(False, [])
        norm = _normalize(text)

        if self.mode == "phrase":
            assert self._phrase_re is not None
            if self._phrase_re.search(norm):
                # Report the phrase as a single joined keyword.
                return MatchResult(True, [" ".join(self.keywords)])
            return MatchResult(False, [])

        assert self._per_keyword is not None
        hits = [
            kw for kw, pat in zip(self.keywords, self._per_keyword) if pat.search(norm)
        ]
        if self.mode == "all":
            matched = len(hits) == len(self.keywords)
        else:  # "any"
            matched = bool(hits)
        return MatchResult(matched, hits if matched else [])

    def search_fields(
        self, *, title: str | None = None, body: str | None = None
    ) -> tuple[bool, list[str], str | None]:
        """Match across title and body, returning (matched, keywords, match_field).

        `match_field` is "title", "body", "both", or None when nothing matched.
        Under `all` mode a match requires the keywords to be satisfied within a
        single field (title or body), matching how a reader would read it.
        """
        t = self.search(title)
        b = self.search(body)

        if not t.matched and not b.matched:
            return False, [], None

        # Merge distinct keywords preserving order.
        merged: list[str] = []
        for kw in t.keywords + b.keywords:
            if kw not in merged:
                merged.append(kw)

        if t.matched and b.matched:
            field = "both"
        elif t.matched:
            field = "title"
        else:
            field = "body"
        return True, merged, field
