"""Page discovery over a fake Notion client: no network, no tokens.

Covers the dashboard layout that broke the first real run: databases nested in
columns and callouts, whose rows are where the actual knowledge lives.
"""

from __future__ import annotations

import logging

import pytest
from notion_client.errors import UnknownHTTPResponseError

import prepare

ROOT = "root0000000000000000000000000000"
DB = "db000000000000000000000000000000"
EXCLUDED = "excluded00000000000000000000000000"
EXCLUDED_CHILD = "excludedchild00000000000000000000"
EXCLUDED_DB = "excludeddb000000000000000000000000"
EXCLUDED_DS = "excludedds000000000000000000000000"
EXCLUDED_ROW = "excludedrow0000000000000000000000"
SIBLING = "sibling0000000000000000000000000"


def page_obj(
    page_id: str, title: str, *, props: dict | None = None, parent: dict | None = None
) -> dict:
    properties = {"名前": {"type": "title", "title": [{"plain_text": title}]}}
    properties.update(props or {})
    page = {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": "2026-10-01T00:00:00.000Z",
        "properties": properties,
    }
    if parent is not None:
        page["parent"] = parent
    return page


def blk(block_id: str, btype: str, *, has_children: bool = False, **body) -> dict:
    return {"id": block_id, "type": btype, "has_children": has_children, btype: body}


@pytest.fixture(autouse=True)
def isolated_block_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.config, "BLOCK_CACHE_DIR", tmp_path / "blocks")


class FakeNotion:
    def __init__(self, pages, children, databases, rows, search_results=(), blocks=None):
        self._pages, self._children = pages, children
        self._databases, self._rows = databases, rows
        self._blocks = blocks or {}
        self.children_calls = []
        self.search_results = list(search_results)
        self.pages = self._Endpoint(retrieve=self._retrieve_page)
        self.databases = self._Endpoint(retrieve=self._retrieve_database)
        self.data_sources = self._Endpoint(query=self._query, retrieve=self._retrieve_data_source)
        self.blocks = self._Endpoint(
            children=self._Endpoint(list=self._list_children), retrieve=self._retrieve_block
        )

    class _Endpoint:
        def __init__(self, **methods):
            self.__dict__.update(methods)

    def search(self, page_size=100, start_cursor=None):
        return {"results": self.search_results, "has_more": False}

    def _retrieve_page(self, page_id):
        if page_id not in self._pages:
            raise prepare.APIResponseError.__new__(prepare.APIResponseError)
        return self._pages[page_id]

    def _retrieve_database(self, database_id):
        if database_id not in self._databases:
            raise prepare.APIResponseError.__new__(prepare.APIResponseError)
        return self._databases[database_id]

    def _retrieve_data_source(self, data_source_id):
        for database_id, database in self._databases.items():
            for source in database.get("data_sources", []):
                if source["id"] == data_source_id:
                    return {
                        "object": "data_source",
                        "id": data_source_id,
                        "parent": source.get(
                            "parent", {"type": "database_id", "database_id": database_id}
                        ),
                    }
        raise prepare.APIResponseError.__new__(prepare.APIResponseError)

    def _retrieve_block(self, block_id):
        if block_id not in self._blocks:
            raise prepare.APIResponseError.__new__(prepare.APIResponseError)
        return self._blocks[block_id]

    def _query(self, data_source_id, page_size=100, start_cursor=None):
        return {"results": self._rows[data_source_id], "has_more": False}

    def _list_children(self, block_id, start_cursor=None, page_size=100):
        self.children_calls.append(block_id)
        return {"results": self._children.get(block_id, []), "has_more": False}


@pytest.fixture
def fake(monkeypatch):
    row = page_obj(
        "row1", "ある論文", props={"著者": {"type": "rich_text", "rich_text": [{"plain_text": "山田"}]}}
    )
    notion = FakeNotion(
        pages={ROOT: page_obj(ROOT, "研究管理", parent={"type": "workspace"})},
        children={
            # The database sits two levels down: column_list > column > child_database.
            ROOT: [blk("cols", "column_list", has_children=True)],
            "cols": [blk("col", "column", has_children=True)],
            "col": [blk(DB, "child_database", title="論文管理_DB")],
            "row1": [blk("p1", "paragraph", rich_text=[{"plain_text": "本文"}])],
        },
        databases={
            DB: {
                "title": [{"plain_text": "論文管理_DB"}],
                "parent": {"type": "page_id", "page_id": ROOT},
                "data_sources": [{"id": "ds1"}],
            }
        },
        rows={"ds1": [row]},
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)
    return notion


def test_nested_database_rows_are_discovered(fake):
    pages = prepare.discover_pages([ROOT])
    titles = [page["title"] for page in pages]
    assert titles == ["研究管理", "ある論文"]
    row = pages[1]
    assert row["database"] == "論文管理_DB"
    assert "著者" in row["properties"]


def test_root_can_be_a_database(fake):
    pages = prepare.discover_pages([DB])
    assert [page["title"] for page in pages] == ["ある論文"]


def test_row_record_contains_properties_and_body(fake):
    row = prepare.discover_pages([ROOT])[1]
    records = prepare.process_page(row, skip_images=True)
    text = records[0]["text"]
    assert "# ある論文" in text
    assert "- 著者: 山田" in text
    assert "本文" in text
    assert records[0]["chunker_version"] == prepare.config.CHUNKER_VERSION


def test_image_metadata_is_computed_after_packing():
    records = prepare.process_page(
        {
            "id": "image-page",
            "title": "画像ページ",
            "url": "https://www.notion.so/image-page",
            "last_edited": "2026-10-01T00:00:00.000Z",
            "blocks": [
                {
                    "id": "image-block",
                    "type": "image",
                    "image": {"type": "file", "file": {"url": "https://example.invalid/image.png"}},
                },
                {
                    "id": "heading-block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"plain_text": "本文"}]},
                },
                {
                    "id": "body-block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "画像の説明"}]},
                },
            ],
        },
        skip_images=True,
    )

    assert len(records) == 1
    assert records[0]["has_image"] is True
    assert "画像リンク:" not in records[0]["image_links"]
    assert "https://www.notion.so/imagepage#imageblock" in records[0]["image_links"]


def test_excluded_database_is_skipped(fake, monkeypatch):
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [DB])
    pages = prepare.discover_pages([ROOT])
    assert [page["title"] for page in pages] == ["研究管理"]


def test_no_roots_uses_everything_connected(fake):
    # Search mode is metadata-only. Nested databases are discovered in root
    # mode; search mode gets database rows from search's connected databases.
    fake.search_results = [{"object": "page", "id": ROOT}]
    pages = prepare.discover_pages([])
    assert [page["title"] for page in pages] == ["研究管理"]
    assert fake.children_calls == []


def test_search_phase_a_does_not_fetch_block_children(fake):
    fake.search_results = [{"object": "page", "id": ROOT}]

    pages = prepare.discover_pages([])

    assert pages[0]["title"] == "研究管理"
    assert "blocks" not in pages[0]
    assert fake.children_calls == []


def test_dry_run_root_phase_a_does_not_fetch_block_children(fake, monkeypatch, capsys):
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [ROOT])
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--dry-run"])

    assert prepare.main() == 0
    assert fake.children_calls == []
    assert "Tier 3: 1 ページ" in capsys.readouterr().out


def test_data_source_in_search_walks_its_database(fake):
    fake.search_results = [
        {"object": "data_source", "id": "ds1", "parent": {"type": "database_id", "database_id": DB}}
    ]
    pages = prepare.discover_pages([])
    assert [page["title"] for page in pages] == ["ある論文"]
    assert pages[0]["database"] == "論文管理_DB"


def test_skip_content_rows_are_not_candidates_in_root_or_search(fake, monkeypatch, caplog):
    row = fake._rows["ds1"][0]
    row["parent"] = {
        "type": "data_source_id",
        "data_source_id": "ds1",
        "database_id": DB,
    }
    fake._pages["row1"] = row
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "SKIP_CONTENT_IDS", [DB, "ds1"])
    caplog.set_level(logging.INFO)

    root_pages = prepare.discover_pages([ROOT])

    assert [page["title"] for page in root_pages] == ["研究管理"]
    assert "row1" not in fake.children_calls
    assert prepare.discover_pages.last_discovered_page_count == 1
    assert sum("skipped data source by config: ds1 (1 rows)" in message for message in caplog.messages) == 1

    fake.search_results = [
        {
            "object": "data_source",
            "id": "ds1",
            "parent": {"type": "database_id", "database_id": DB},
        },
        row,
    ]
    search_pages = prepare.discover_pages([])

    assert search_pages == []
    assert prepare.discover_pages.last_discovered_page_count == 0


def test_row_found_by_search_keeps_its_properties(fake):
    row = fake._rows["ds1"][0]
    row["parent"] = {"type": "data_source_id", "data_source_id": "ds1"}
    fake._pages["row1"] = row
    fake.search_results = [{"object": "page", "id": "row1"}]
    pages = prepare.discover_pages([])
    assert "著者" in pages[0]["properties"]
    assert pages[0]["database"] == "論文管理_DB"


def test_row_reached_by_id_gets_its_database_title(fake):
    row = fake._rows["ds1"][0]
    row["parent"] = {"type": "data_source_id", "data_source_id": "ds1"}
    fake._pages["row1"] = row

    pages = prepare.discover_pages(["row1"])

    assert pages[0]["database"] == "論文管理_DB"
    assert "著者" in pages[0]["properties"]


def test_explicit_roots_do_not_call_search(fake):
    fake.search_results = [{"object": "page", "id": "should-not-be-used"}]
    pages = prepare.discover_pages([ROOT])
    assert all(page["id"] != "should-not-be-used" for page in pages)


def test_excluded_page_subtree_is_skipped_but_sibling_is_kept(monkeypatch):
    row = page_obj(
        EXCLUDED_ROW,
        "除外DBの行",
        parent={"type": "data_source_id", "data_source_id": EXCLUDED_DS},
    )
    notion = FakeNotion(
        pages={
            ROOT: page_obj(ROOT, "ルート", parent={"type": "workspace"}),
            EXCLUDED: page_obj(EXCLUDED, "除外ページ", parent={"type": "page_id", "page_id": ROOT}),
            EXCLUDED_CHILD: page_obj(
                EXCLUDED_CHILD,
                "除外ページの子",
                parent={"type": "page_id", "page_id": EXCLUDED},
            ),
            SIBLING: page_obj(SIBLING, "除外ページの兄弟", parent={"type": "page_id", "page_id": ROOT}),
        },
        children={
            ROOT: [blk(EXCLUDED, "child_page"), blk(SIBLING, "child_page")],
            EXCLUDED: [blk(EXCLUDED_CHILD, "child_page"), blk(EXCLUDED_DB, "child_database")],
            SIBLING: [],
            EXCLUDED_ROW: [],
        },
        databases={
            EXCLUDED_DB: {
                "title": [{"plain_text": "除外DB"}],
                "parent": {"type": "page_id", "page_id": EXCLUDED},
                "data_sources": [{"id": EXCLUDED_DS}],
            }
        },
        rows={EXCLUDED_DS: [row]},
        search_results=[
            {"object": "page", "id": ROOT},
            {"object": "page", "id": EXCLUDED},
            {"object": "page", "id": EXCLUDED_CHILD},
            {"object": "page", "id": SIBLING},
            {
                "object": "data_source",
                "id": EXCLUDED_DS,
                "parent": {"type": "database_id", "database_id": EXCLUDED_DB},
            },
        ],
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [EXCLUDED])

    pages = prepare.discover_pages([])

    assert [page["title"] for page in pages] == ["ルート", "除外ページの兄弟"]


def test_search_root_database_row_is_skipped_when_database_ancestor_is_excluded(
    fake, monkeypatch
):
    row = fake._rows["ds1"][0]
    row["parent"] = {"type": "data_source_id", "data_source_id": "ds1"}
    fake.search_results = [{"object": "page", "id": "row1"}]
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [DB])

    assert prepare.discover_pages([]) == []


def test_unreachable_page_ancestor_does_not_drop_a_page(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    child = page_obj(
        "child",
        "子ページ",
        parent={"type": "page_id", "page_id": "unreachable-parent"},
    )
    notion = FakeNotion(
        pages={ROOT: page_obj(ROOT, "ルート", parent={"type": "workspace"}), "child": child},
        children={ROOT: [blk("child", "child_page")], "child": []},
        databases={"unused": {"title": [], "data_sources": []}},
        rows={},
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)

    pages = prepare.discover_pages([ROOT])

    assert [page["title"] for page in pages] == ["ルート", "子ページ"]
    assert sum("祖先 page:" in message for message in caplog.messages) == 1


def test_unreachable_database_ancestor_does_not_drop_the_page(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    notion = FakeNotion(
        pages={ROOT: page_obj(ROOT, "ルート", parent={"type": "workspace"})},
        children={ROOT: []},
        databases={},
        rows={},
    )
    notion._pages[ROOT]["parent"] = {"type": "database_id", "database_id": "unreachable-db"}
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)

    pages = prepare.discover_pages([ROOT])

    assert [page["title"] for page in pages] == ["ルート"]
    assert pages[0]["database"] == ""
    assert any("祖先 database:" in message for message in caplog.messages)


def test_unreachable_database_ancestor_does_not_drop_database_rows(fake, caplog):
    caplog.set_level(logging.INFO)
    fake._databases[DB]["parent"] = {
        "type": "page_id",
        "page_id": "unreachable-parent",
    }

    pages = prepare.discover_pages([DB])

    assert [page["title"] for page in pages] == ["ある論文"]
    assert pages[0]["database"] == "論文管理_DB"
    assert any("祖先 page:" in message for message in caplog.messages)


def test_unreachable_block_ancestor_does_not_drop_the_page(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    notion = FakeNotion(
        pages={ROOT: page_obj(ROOT, "ルート", parent={"type": "block_id", "block_id": "unseen-block"})},
        children={ROOT: []},
        databases={},
        rows={},
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)

    pages = prepare.discover_pages([ROOT])

    assert [page["title"] for page in pages] == ["ルート"]
    assert any("祖先 block:" in message for message in caplog.messages)


def test_block_parent_is_followed_for_exclusion(monkeypatch):
    nested = page_obj(
        "nested",
        "トグル内ページ",
        parent={"type": "block_id", "block_id": "toggle"},
    )
    notion = FakeNotion(
        pages={"nested": nested},
        children={"nested": []},
        databases={},
        rows={},
        blocks={
            "toggle": {
                "object": "block",
                "id": "toggle",
                "parent": {"type": "page_id", "page_id": ROOT},
            }
        },
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [ROOT])

    assert prepare.discover_pages(["nested"]) == []


def test_root_failure_is_reported_with_actionable_message(caplog, monkeypatch):
    notion = FakeNotion(pages={}, children={}, databases={}, rows={})
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare, "_throttle", lambda: None)

    assert prepare.discover_pages(["missing-root"]) == []
    assert "ページを取得できません" in caplog.text
    assert "Notion で「接続を追加」をしたか確認" in caplog.text
    assert prepare.discover_pages.last_failed_roots == ["missing-root"]


def test_retry_wrapper_retries_502_then_succeeds(monkeypatch, caplog):
    calls = 0

    class Children:
        def list(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UnknownHTTPResponseError(502)
            return {"results": [], "has_more": False}

    class Blocks:
        children = Children()

    class Notion:
        blocks = Blocks()

    monkeypatch.setattr(prepare, "_get_notion", lambda: Notion())
    monkeypatch.setattr(prepare, "_throttle", lambda: None)
    monkeypatch.setattr(prepare.time, "sleep", lambda _: None)
    monkeypatch.setattr(prepare.random, "random", lambda: 0.0)

    assert prepare.fetch_block_children(ROOT) == []
    assert calls == 2
    assert "status=502" in caplog.text
    assert "attempt=1/6" in caplog.text


def test_retry_wrapper_gives_up_after_max_attempts(monkeypatch):
    calls = 0

    class Children:
        def list(self, **kwargs):
            nonlocal calls
            calls += 1
            raise UnknownHTTPResponseError(502)

    class Blocks:
        children = Children()

    class Notion:
        blocks = Blocks()

    monkeypatch.setattr(prepare, "_get_notion", lambda: Notion())
    monkeypatch.setattr(prepare, "_throttle", lambda: None)
    monkeypatch.setattr(prepare.time, "sleep", lambda _: None)
    monkeypatch.setattr(prepare.config, "API_MAX_RETRIES", 2)

    with pytest.raises(UnknownHTTPResponseError):
        prepare.fetch_block_children(ROOT)
    assert calls == 3


def test_page_block_failure_is_skipped_and_recorded(fake, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)

    def fail(_page_id):
        raise UnknownHTTPResponseError(502)

    monkeypatch.setattr(prepare, "fetch_block_children", fail)

    assert prepare.discover_pages([ROOT], use_block_cache=False) == []
    assert prepare.discover_pages.last_failed_pages == [ROOT]
    assert prepare.discover_pages.last_failed_page_titles == ["研究管理"]
    assert "id=root0000000000000000000000000000" in caplog.text


def test_block_cache_hit_skips_fetch_and_changed_page_refetches(fake, monkeypatch):
    calls = 0
    original_fetch = prepare.fetch_block_children

    def counting_fetch(page_id):
        nonlocal calls
        calls += 1
        return original_fetch(page_id)

    monkeypatch.setattr(prepare, "fetch_block_children", counting_fetch)

    first = prepare.discover_pages([ROOT])
    first_calls = calls
    second = prepare.discover_pages([ROOT])
    assert first[0]["blocks"] == second[0]["blocks"]
    assert calls == first_calls

    fake._pages[ROOT]["last_edited_time"] = "2026-10-02T00:00:00.000Z"
    prepare.discover_pages([ROOT])
    assert calls > first_calls
