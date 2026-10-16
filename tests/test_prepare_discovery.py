"""Page discovery over a fake Notion client: no network, no tokens.

Covers the dashboard layout that broke the first real run: databases nested in
columns and callouts, whose rows are where the actual knowledge lives.
"""

from __future__ import annotations

import pytest

import prepare

ROOT = "root0000000000000000000000000000"
DB = "db000000000000000000000000000000"


def page_obj(page_id: str, title: str, *, props: dict | None = None) -> dict:
    properties = {"名前": {"type": "title", "title": [{"plain_text": title}]}}
    properties.update(props or {})
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": "2026-10-01T00:00:00.000Z",
        "properties": properties,
    }


def blk(block_id: str, btype: str, *, has_children: bool = False, **body) -> dict:
    return {"id": block_id, "type": btype, "has_children": has_children, btype: body}


class FakeNotion:
    def __init__(self, pages, children, databases, rows):
        self._pages, self._children = pages, children
        self._databases, self._rows = databases, rows
        self.pages = self._Endpoint(retrieve=self._retrieve_page)
        self.databases = self._Endpoint(retrieve=self._retrieve_database)
        self.data_sources = self._Endpoint(query=self._query)
        self.blocks = self._Endpoint(children=self._Endpoint(list=self._list_children))

    class _Endpoint:
        def __init__(self, **methods):
            self.__dict__.update(methods)

    def _retrieve_page(self, page_id):
        if page_id not in self._pages:
            raise prepare.APIResponseError.__new__(prepare.APIResponseError)
        return self._pages[page_id]

    def _retrieve_database(self, database_id):
        if database_id not in self._databases:
            raise prepare.APIResponseError.__new__(prepare.APIResponseError)
        return self._databases[database_id]

    def _query(self, data_source_id, page_size=100, start_cursor=None):
        return {"results": self._rows[data_source_id], "has_more": False}

    def _list_children(self, block_id, start_cursor=None, page_size=100):
        return {"results": self._children.get(block_id, []), "has_more": False}


@pytest.fixture
def fake(monkeypatch):
    row = page_obj(
        "row1", "ある論文", props={"著者": {"type": "rich_text", "rich_text": [{"plain_text": "山田"}]}}
    )
    notion = FakeNotion(
        pages={ROOT: page_obj(ROOT, "研究管理")},
        children={
            # The database sits two levels down: column_list > column > child_database.
            ROOT: [blk("cols", "column_list", has_children=True)],
            "cols": [blk("col", "column", has_children=True)],
            "col": [blk(DB, "child_database", title="論文管理_DB")],
            "row1": [blk("p1", "paragraph", rich_text=[{"plain_text": "本文"}])],
        },
        databases={
            DB: {"title": [{"plain_text": "論文管理_DB"}], "data_sources": [{"id": "ds1"}]}
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


def test_excluded_database_is_skipped(fake, monkeypatch):
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [DB])
    pages = prepare.discover_pages([ROOT])
    assert [page["title"] for page in pages] == ["研究管理"]
