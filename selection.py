"""Pure page prioritization and budget selection helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import re

import config


_DATE_TITLE_PATTERNS = (
    re.compile(r"(?<!\d)\d{4}(?P<separator>[./-])\d{1,2}(?P=separator)\d{1,2}(?!\d)"),
    re.compile(r"(?<!\d)\d{8}(?!\d)"),
    re.compile(r"^\s*\d{1,2}/\d{1,2}(?!\d)"),
    re.compile(r"(?<!\d)\d{4}年\d{1,2}月\d{1,2}日(?!\d)"),
    re.compile(r"(?<!\d)\d{1,2}月\d{1,2}日(?!\d)"),
)

_MT_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z])(?:[A-Z]{0,3})MT(?![A-Za-z])")
_MTG_TOKEN_PATTERN = re.compile(r"(?i)(?<![a-z])mtg(?![a-z])")


def _page_text(page: dict) -> tuple[str, str]:
    title = str(page.get("title") or "")
    database = str(page.get("database") or "")
    return title, database


def _contains_keyword(text: str, keywords: list[str]) -> bool:
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def _has_date_title(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DATE_TITLE_PATTERNS)


def _has_tier4_keyword(text: str) -> bool:
    if _MT_TOKEN_PATTERN.search(text) or _MTG_TOKEN_PATTERN.search(text):
        return True
    japanese_keywords = [
        keyword
        for keyword in config.TIER4_KEYWORDS
        if keyword not in {"MT", "mtg", "MTG"}
    ]
    return _contains_keyword(text, japanese_keywords)


def classify(page: dict) -> int | None:
    """Return the page tier, or ``None`` when the page must be excluded."""
    title, database = _page_text(page)
    combined = f"{title}\n{database}"

    if _contains_keyword(combined, config.EXCLUDE_TITLE_KEYWORDS):
        return None
    if _contains_keyword(combined, config.TIER1_KEYWORDS):
        return 1
    if _contains_keyword(combined, config.TIER2_KEYWORDS):
        return 2
    if _has_tier4_keyword(combined):
        return 4
    if _has_date_title(title) or _has_date_title(database):
        return 4
    return 3


def _timestamp_value(value: object) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def order_pages(pages: list[dict]) -> list[dict]:
    """Drop excluded pages and order the rest by tier, freshness, and title."""
    eligible = []
    for page in pages:
        page["tier"] = classify(page)
        if page["tier"] is not None:
            eligible.append(page)
    return sorted(
        eligible,
        key=lambda page: (
            page["tier"],
            -_timestamp_value(page.get("last_edited", "")),
            page.get("title", ""),
        ),
    )


class IncrementalBudgetSelector:
    """Greedy budget selection for pages whose chunk counts arrive lazily."""

    def __init__(self, budget: int):
        if budget < 0:
            raise ValueError("budget must be non-negative")
        self.budget = budget
        self.total = 0
        self.selected_ids: list[str] = []

    def try_add(self, page_id: str, n_chunks: int) -> bool:
        """Add a page when it fits and return whether it was selected."""
        if n_chunks < 0:
            raise ValueError("n_chunks must be non-negative")
        if self.budget and self.total + n_chunks > self.budget:
            return False
        self.selected_ids.append(page_id)
        self.total += n_chunks
        return True


def select_within_budget(
    ordered: list[dict], chunk_counts: dict[str, int], budget: int
) -> tuple[list[dict], int]:
    """Greedily select pages, skipping oversized pages and continuing.

    A zero budget is the command-line convention for an unlimited budget.
    """
    selector = IncrementalBudgetSelector(budget)
    for page in ordered:
        selector.try_add(page["id"], chunk_counts[page["id"]])
    selected_ids = set(selector.selected_ids)
    return [page for page in ordered if page["id"] in selected_ids], selector.total
