"""Tests for tag-aware Notion ingest and Vectorize metadata."""

from __future__ import annotations

import json

import pytest

import config
import index_build
import notion_md
import prepare
import selection
import vectorize_client


TAG_SOURCE = "tag-source-0000000000000000000000000000"
TAG_ALPHA = "tag-alpha-000000000000000000000000000"
TAG_BETA = "tag-beta-00000000000000000000000000000"
TAG_UNKNOWN = "tag-unknown-00000000000000000000000000"
ROW_DATA_SOURCE = "row-source-00000000000000000000000000"
ROW_DATABASE = "row-database-0000000000000000000000000"
TAG_DATABASE = "tag-database-000000000000000000000000"


def title_property(title: str) -> dict:
    return {"名前": {"type": "title", "title": [{"plain_text": title}]}}


def notion_page(
    page_id: str,
    title: str,
    *,
    properties: dict | None = None,
    parent: dict | None = None,
    last_edited: str = "2026-11-01T00:00:00.000Z",
) -> dict:
    page = {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": last_edited,
        "properties": {**title_property(title), **(properties or {})},
    }
    if parent is not None:
        page["parent"] = parent
    return page


class _Endpoint:
    def __init__(self, **methods):
        self.__dict__.update(methods)


class FakeNotion:
    """Small Notion double covering only the reads used by the tests."""

    def __init__(
        self,
        *,
        search_results: list[dict] | None = None,
        data_source_rows: dict[str, list[dict]] | None = None,
        databases: dict[str, dict] | None = None,
        property_pages: dict[tuple[str, str], list[dict]] | None = None,
    ):
        self.search_results = search_results or []
        self.data_source_rows = data_source_rows or {}
        self.database_rows = databases or {}
        self.property_pages = property_pages or {}
        self.query_calls: list[str] = []
        self.property_calls: list[dict] = []
        self.children_calls: list[str] = []

        self.pages = _Endpoint(
            retrieve=self._retrieve_page,
            properties=_Endpoint(retrieve=self._retrieve_property),
        )
        self.data_sources = _Endpoint(
            query=self._query_data_source,
            retrieve=self._retrieve_data_source,
        )
        self.databases = _Endpoint(retrieve=self._retrieve_database)
        self.blocks = _Endpoint(
            children=_Endpoint(list=self._list_children),
            retrieve=self._retrieve_block,
        )

    def search(self, **kwargs) -> dict:
        return {"results": self.search_results, "has_more": False}

    def _retrieve_page(self, page_id: str) -> dict:
        raise AssertionError(f"unexpected pages.retrieve: {page_id}")

    def _retrieve_property(self, **kwargs) -> dict:
        self.property_calls.append(kwargs)
        key = (kwargs["page_id"], kwargs["property_id"])
        pages = self.property_pages[key]
        if kwargs.get("start_cursor"):
            return pages[1]
        return pages[0]

    def _query_data_source(self, data_source_id: str, **kwargs) -> dict:
        self.query_calls.append(data_source_id)
        return {"results": self.data_source_rows.get(data_source_id, []), "has_more": False}

    def _retrieve_data_source(self, data_source_id: str) -> dict:
        return {"object": "data_source", "id": data_source_id}

    def _retrieve_database(self, database_id: str) -> dict:
        return self.database_rows[database_id]

    def _retrieve_block(self, block_id: str) -> dict:
        raise AssertionError(f"unexpected blocks.retrieve: {block_id}")

    def _list_children(self, block_id: str, **kwargs) -> dict:
        self.children_calls.append(block_id)
        raise AssertionError(f"unexpected blocks.children.list: {block_id}")


@pytest.fixture(autouse=True)
def no_notion_sleep(monkeypatch):
    monkeypatch.setattr(prepare, "_throttle", lambda: None)


def test_tag_relations_resolve_names_once_and_follow_property_pagination(monkeypatch):
    page_one = notion_page(
        "page-one-000000000000000000000000000",
        "One",
        properties={
            "Tags": {
                "id": "tags-property",
                "type": "relation",
                "relation": {
                    "data_source_id": TAG_SOURCE,
                    "results": [{"id": TAG_ALPHA}],
                },
                "has_more": True,
            }
        },
        parent={"type": "workspace"},
    )
    page_two = notion_page(
        "page-two-000000000000000000000000000",
        "Two",
        properties={
            "Tag": {
                "type": "relation",
                "relation": {
                    "data_source_id": TAG_SOURCE,
                    "results": [{"id": TAG_BETA}, {"id": TAG_UNKNOWN}],
                },
            }
        },
        parent={"type": "workspace"},
    )
    page_three = notion_page(
        "page-three-00000000000000000000000000",
        "Three",
        properties={
            "Tags": {
                "type": "relation",
                "relation": {
                    "data_source_id": TAG_SOURCE,
                    "results": [{"id": TAG_ALPHA}],
                },
            }
        },
        parent={"type": "workspace"},
    )
    tag_rows = [
        notion_page(TAG_ALPHA, "Alpha", parent={"type": "workspace"}),
        notion_page(TAG_BETA, "Beta", parent={"type": "workspace"}),
    ]
    notion = FakeNotion(
        search_results=[page_one, page_two, page_three],
        data_source_rows={TAG_SOURCE: tag_rows},
        property_pages={
            (page_one["id"], "tags-property"): [
                {
                    "results": [
                        {
                            "object": "property_item",
                            "type": "relation",
                            "relation": {"id": TAG_BETA},
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                },
                {
                    "results": [
                        {
                            "object": "property_item",
                            "type": "relation",
                            "relation": {"id": TAG_UNKNOWN},
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            ]
        },
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [])

    pages = prepare.discover_pages([])

    assert [page["tags"] for page in pages] == [["Alpha", "Beta"], ["Beta"], ["Alpha"]]
    assert notion.query_calls == [TAG_SOURCE]
    assert [call.get("start_cursor") for call in notion.property_calls] == [None, "cursor-2"]
    assert all(TAG_UNKNOWN not in page["tags"] for page in pages)


def test_skipped_tag_data_source_is_not_candidate_but_resolves_tag_names(
    monkeypatch, caplog
):
    tag_row = notion_page(
        TAG_ALPHA,
        "インフラ",
        parent={
            "type": "data_source_id",
            "data_source_id": TAG_SOURCE,
            "database_id": TAG_DATABASE,
        },
    )
    content_page = notion_page(
        "content-page-0000000000000000000000000",
        "Content",
        properties={
            "Tags": {
                "type": "relation",
                "relation": {
                    "data_source_id": TAG_SOURCE,
                    "results": [{"id": TAG_ALPHA}],
                },
            }
        },
        parent={"type": "workspace"},
    )
    notion = FakeNotion(
        search_results=[
            {
                "object": "data_source",
                "id": TAG_SOURCE,
                "parent": {"type": "database_id", "database_id": TAG_DATABASE},
            },
            content_page,
        ],
        data_source_rows={TAG_SOURCE: [tag_row]},
        databases={
            TAG_DATABASE: {
                "object": "database",
                "id": TAG_DATABASE,
                "title": [{"plain_text": "Tag Library"}],
                "data_sources": [{"id": TAG_SOURCE}],
            }
        },
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "SKIP_CONTENT_IDS", [TAG_SOURCE, TAG_DATABASE])
    caplog.set_level("INFO")

    pages = prepare.discover_pages([])

    assert [page["title"] for page in pages] == ["Content"]
    assert pages[0]["tags"] == ["インフラ"]
    assert notion.query_calls == [TAG_SOURCE]
    assert sum("skipped data source by config" in message for message in caplog.messages) == 1


def test_tag_markdown_uses_names_and_skips_other_relation_ids():
    properties = {
        "Tag": {
            "type": "relation",
            "relation": [{"id": TAG_ALPHA}],
        },
        "Tags": {
            "type": "relation",
            "relation": [{"id": TAG_BETA}],
        },
        "関連ページ": {
            "type": "relation",
            "relation": [{"id": "raw-related-page-id"}],
        },
    }

    lines = notion_md.properties_to_markdown_lines(properties, tags=["Alpha", "Beta"])

    assert lines == ["- Tag: Alpha, Beta", "- Tags: Alpha, Beta"]
    rendered = "\n".join(lines)
    assert TAG_ALPHA not in rendered
    assert TAG_BETA not in rendered
    assert "raw-related-page-id" not in rendered


def test_every_chunk_has_tag_header_without_duplicate_title():
    page = {
        "id": "chunk-page",
        "title": "Chunked page",
        "url": "https://www.notion.so/chunk-page",
        "last_edited": "2026-11-01T00:00:00.000Z",
        "tags": ["a", "b"],
        "blocks": [
            {
                "id": "body",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "本文。" * 1200}]},
            }
        ],
    }

    records = prepare.process_page(page, skip_images=True)

    assert len(records) > 1
    assert records[0]["text"].startswith("# Chunked page")
    assert records[0]["text"].count("# Chunked page") == 1
    for record in records:
        assert "タグ: a, b" in record["text"]
    assert any(record["text"].startswith("[Chunked page] タグ: a, b") for record in records[1:])

    untagged = dict(page, id="untagged", title="Untagged", tags=[])
    untagged_records = prepare.process_page(untagged, skip_images=True)
    assert all(" タグ:" not in record["text"] for record in untagged_records)


def test_tags_metadata_reaches_index_build_vectorize_payload(monkeypatch):
    tag_text = "a, " + "b" * config.TAG_METADATA_MAX_CHARS
    record = {
        "id": "payload-page-0",
        "text": "本文",
        "title": "Payload",
        "url": "https://www.notion.so/payload-page",
        "last_edited": "2026-11-01T00:00:00.000Z",
        "source": "notion",
        "has_image": False,
        "image_links": "",
        "heading_path": "",
        "tags": tag_text,
    }
    payloads: list[str] = []

    monkeypatch.setattr(index_build, "_failed_page_ids", lambda: set())
    monkeypatch.setattr(index_build.vectorize_client, "require_credentials", lambda: None)
    monkeypatch.setattr(index_build.vectorize_client, "load_manifest", lambda: set())
    monkeypatch.setattr(index_build.vectorize_client, "save_manifest", lambda ids: None)
    monkeypatch.setattr(
        index_build.embedder,
        "embed",
        lambda texts: [[0.0] * config.EMBED_DIM for _ in texts],
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "upsert",
        lambda records, vectors, **kwargs: payloads.append(
            vectorize_client.build_ndjson(records, vectors)
        ),
    )

    assert index_build.build_vectorize([record]) == 0

    payload = json.loads(payloads[0])
    assert payload["metadata"]["tags"] == tag_text[: config.TAG_METADATA_MAX_CHARS]
    assert vectorize_client.build_metadata(record)["tags"] == tag_text[
        : config.TAG_METADATA_MAX_CHARS
    ]


def test_selection_tag_precedence_is_case_insensitive_and_exact():
    def page(*, title="plain", database="", tags=None):
        return {"title": title, "database": database, "tags": tags or []}

    assert selection.classify(page(tags=["pj_documents"])) == 1
    assert selection.classify(page(database="BACKEND-KNOWLEDGE", tags=["mt"])) == 1
    assert selection.classify(page(tags=["mt", "docker"])) == 4
    assert selection.classify(page(tags=["DOCKER"])) == 2
    assert selection.classify(page(tags=["Docker Compose"])) == 3


def test_1on1_is_allowed_but_yomoyama_is_excluded():
    assert selection.classify({"title": "1on1", "database": "", "tags": []}) is not None
    assert selection.classify({"title": "よもやま", "database": "", "tags": []}) is None


@pytest.mark.parametrize("excluded_id", [ROW_DATA_SOURCE, ROW_DATABASE])
def test_database_row_is_excluded_by_either_data_source_or_database_parent(
    monkeypatch, excluded_id
):
    row = notion_page(
        "row-page-000000000000000000000000000",
        "Private row",
        parent={
            "type": "data_source_id",
            "data_source_id": ROW_DATA_SOURCE,
            "database_id": ROW_DATABASE,
        },
    )
    notion = FakeNotion(search_results=[row])
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [excluded_id])

    assert prepare.discover_pages([]) == []


def test_excluded_data_source_search_result_is_skipped_before_query(monkeypatch):
    row = notion_page(
        "private-row-00000000000000000000000000",
        "Private row",
        parent={"type": "data_source_id", "data_source_id": ROW_DATA_SOURCE},
    )
    notion = FakeNotion(
        search_results=[
            {
                "object": "data_source",
                "id": ROW_DATA_SOURCE,
                "parent": {"type": "database_id", "database_id": ROW_DATABASE},
            }
        ],
        databases={
            ROW_DATABASE: {
                "object": "database",
                "id": ROW_DATABASE,
                "title": [{"plain_text": "Private DB"}],
                "data_sources": [{"id": ROW_DATA_SOURCE}],
            }
        },
        data_source_rows={ROW_DATA_SOURCE: [row]},
    )
    monkeypatch.setattr(prepare, "_get_notion", lambda: notion)
    monkeypatch.setattr(prepare.config, "NOTION_EXCLUDE_PAGE_IDS", [ROW_DATA_SOURCE])

    assert prepare.discover_pages([]) == []
    assert notion.query_calls == []


def test_chunker_version_mismatch_rechunks_from_block_cache_without_listing_children(
    tmp_path, monkeypatch
):
    page = {
        "id": "cached-page",
        "title": "手順",
        "url": "https://www.notion.so/cached-page",
        "last_edited": "2026-11-01T00:00:00.000Z",
        "database": "",
        "properties": {},
    }
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "BLOCK_CACHE_DIR", tmp_path / "blocks")
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    prepare._write_block_cache(page["id"], page["last_edited"], [{"id": "cached-block"}])
    prepare.config.CLEAN_DIR.mkdir(parents=True)
    (prepare.config.CLEAN_DIR / "cached-page.jsonl").write_text(
        json.dumps(
            {
                "id": "cached-page-0",
                "last_edited": page["last_edited"],
                "chunker_version": config.CHUNKER_VERSION - 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    fetched: list[str] = []
    processed_blocks: list[list[dict]] = []
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: [page])
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )

    def fake_process(content_page, *, skip_images):
        processed_blocks.append(content_page["blocks"])
        return [{"id": "cached-page-0"}]

    monkeypatch.setattr(prepare, "process_page", fake_process)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "1"])

    assert prepare.main() == 0
    assert fetched == []
    assert processed_blocks == [[{"id": "cached-block"}]]
