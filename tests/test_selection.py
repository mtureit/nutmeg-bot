"""Page classification, ordering, and greedy budget selection."""

from __future__ import annotations

import selection


def page(page_id: str, title: str, *, database: str = "", edited: str = "") -> dict:
    return {
        "id": page_id,
        "title": title,
        "database": database,
        "last_edited": edited,
    }


def test_classification_uses_title_and_database_name_case_insensitively():
    assert selection.classify(page("1", "HOW TO deploy")) == 1
    assert selection.classify(page("2", "通常の行", database="議事録")) == 4
    assert selection.classify(page("3", "通常の行", database="雑談ログ")) is None


def test_exclusion_wins_and_tier_one_beats_tier_four():
    assert selection.classify(page("1", "定例の引き継ぎ手順 MTG")) == 1
    assert selection.classify(page("2", "45th GM引き継ぎ資料")) == 1
    assert selection.classify(page("3", "SeeFTデプロイ手順 MT")) == 1
    assert selection.classify(page("4", "アイスブレイクの手順")) is None


def test_date_titled_minutes_are_tier_four():
    for title in (
        "2026/08/19 定例",
        "2026/8/19",
        "2026-8-5",
        "2026.8.5",
        "2026-08-19",
        "2026.08.19",
        "20260819",
        "08/19 定例",
        "8/19 定例",
        "2026年8月19日",
        "8月19日 定例",
    ):
        assert selection.classify(page(title, title, database="通常")) == 4
    assert selection.classify(page("memo", "メモ 08/19", database="通常")) == 3


def test_english_tier_four_keywords_match_tokens_only():
    assert selection.classify(page("smtp", "SMTP サーバ設定")) == 3
    assert selection.classify(page("mtureit", "mtureitメモ")) == 3
    assert selection.classify(page("g mmt", "2026/10/23 GMMT")) == 4
    assert selection.classify(page("pmmt", "PMMT")) == 4
    assert selection.classify(page("nutmeg", "NUTMEG-Seeds 2026/10/08 MT")) == 4
    assert selection.classify(page("mtg", "週次 mtg")) == 4


def test_ordering_drops_excluded_and_prioritizes_fresh_pages_within_tier():
    pages = [
        page("old-tier1", "手順 A", edited="2026-01-01T00:00:00Z"),
        page("new-tier1", "手順 B", edited="2026-02-01T00:00:00Z"),
        page("tier2", "企画", edited="2026-12-01T00:00:00Z"),
        page("tier3", "一般", edited="2026-12-02T00:00:00Z"),
        page("excluded", "雑談", edited="2026-12-31T00:00:00Z"),
    ]

    ordered = selection.order_pages(pages)
    assert [item["id"] for item in ordered] == [
        "new-tier1",
        "old-tier1",
        "tier2",
        "tier3",
    ]
    assert {item["id"]: item["tier"] for item in pages} == {
        "old-tier1": 1,
        "new-tier1": 1,
        "tier2": 2,
        "tier3": 3,
        "excluded": None,
    }


def test_order_pages_classifies_each_page_once(monkeypatch):
    pages = [page("a", "A"), page("b", "B")]
    calls: list[str] = []

    def fake_classify(item):
        calls.append(item["id"])
        return 3

    monkeypatch.setattr(selection, "classify", fake_classify)

    selection.order_pages(pages)

    assert calls == ["a", "b"]
    assert [item["tier"] for item in pages] == [3, 3]


def test_budget_skips_large_pages_and_continues():
    ordered = [page("a", "A"), page("b", "B"), page("c", "C"), page("d", "D")]
    selected, total = selection.select_within_budget(
        ordered, {"a": 4, "b": 7, "c": 3, "d": 1}, budget=8
    )

    assert [item["id"] for item in selected] == ["a", "c", "d"]
    assert total == 8


def test_budget_exact_boundary_and_zero_unlimited():
    ordered = [page("a", "A"), page("b", "B")]
    counts = {"a": 4, "b": 3}

    selected, total = selection.select_within_budget(ordered, counts, budget=7)
    assert [item["id"] for item in selected] == ["a", "b"]
    assert total == 7

    selected, total = selection.select_within_budget(ordered, counts, budget=0)
    assert [item["id"] for item in selected] == ["a", "b"]
    assert total == 7


def test_incremental_budget_selector_matches_greedy_rules():
    selector = selection.IncrementalBudgetSelector(5)

    assert selector.try_add("a", 4) is True
    assert selector.try_add("b", 2) is False
    assert selector.try_add("c", 1) is True
    assert selector.selected_ids == ["a", "c"]
    assert selector.total == 5
