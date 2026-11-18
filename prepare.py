"""Phase 1-1: Notion -> Markdown -> chunks -> JSONL (PLAN.md 10).

Page discovery starts from a set of roots and walks down (child pages, nested
databases and their rows).

- NOTION_ROOT_PAGE_IDS set: only those roots. The ingest boundary is written
  down outside the code (PLAN.md 10.0, 17.1).
- NOTION_ROOT_PAGE_IDS empty: the roots are everything the search API returns,
  i.e. everything connected to the integration, so NOTION_TOKEN alone is enough.
  The scope then follows whatever people connect in Notion; check it with
  --dry-run. Search results and connected database rows are collected as
  metadata first, and page content is fetched later in selection order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import random
import sys
import tempfile
import time

import httpx
from notion_client import Client, RetryOptions
from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError

import chunking
import config
import notion_md
import redact
import selection
import vision_client

logger = logging.getLogger(__name__)

_notion: Client | None = None

_NOTION_RETRY_STATUSES = {502, 504}
_NOTION_SDK_RETRY_STATUSES = {429, 500, 503}
_NOTION_READ_ERRORS = (HTTPResponseError, RequestTimeoutError, httpx.RequestError)


def _error_status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else None


def _is_access_error(error: BaseException) -> bool:
    status = _error_status(error)
    # Small test doubles historically used an uninitialized APIResponseError
    # for a missing object; real responses always carry an HTTP status.
    return status in (403, 404) or (status is None and isinstance(error, APIResponseError))


def _notion_call(operation: str, callback, *, sdk_retries: bool = True):
    """Call a Notion read and retry 502/504 or transport errors.

    notion-client's RetryOptions handles 429/500/503 inside the client. This
    wrapper fills the gap for gateway errors and errors that happen before an
    HTTP response exists. Keeping it around every read also makes fake clients
    used by tests behave like the real client.
    """
    max_attempts = max(1, config.API_MAX_RETRIES + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            return callback()
        except _NOTION_READ_ERRORS as exc:
            status = _error_status(exc)
            retryable = (
                status in _NOTION_RETRY_STATUSES
                or (not sdk_retries and status in _NOTION_SDK_RETRY_STATUSES)
                or isinstance(exc, (RequestTimeoutError, httpx.RequestError))
            )
            if not retryable or attempt >= max_attempts:
                raise

            base_delay = min(
                config.NOTION_INITIAL_RETRY_DELAY_SEC * (2 ** (attempt - 1)),
                config.NOTION_MAX_RETRY_DELAY_SEC,
            )
            delay = min(
                config.NOTION_MAX_RETRY_DELAY_SEC,
                base_delay * (0.5 + random.random()),
            )
            logger.warning(
                "Notion read retry: operation=%s status=%s attempt=%d/%d",
                operation,
                status or type(exc).__name__,
                attempt,
                max_attempts,
            )
            time.sleep(delay)


def _get_notion() -> Client:
    global _notion
    if _notion is None:
        config.require("NOTION_TOKEN")
        # notion-client ignores keyword arguments such as auth when options is
        # given, so the token must live inside options.
        _notion = Client(
            options={
                "auth": config.NOTION_TOKEN,
                "retry": RetryOptions(
                    max_retries=config.API_MAX_RETRIES,
                    initial_retry_delay_ms=int(config.NOTION_INITIAL_RETRY_DELAY_SEC * 1000),
                    max_retry_delay_ms=int(config.NOTION_MAX_RETRY_DELAY_SEC * 1000),
                )
            },
        )
    return _notion


def _throttle() -> None:
    """Notion allows roughly 3 requests/second on average."""
    time.sleep(config.NOTION_SLEEP_SEC)


def _uses_sdk_retries(notion) -> bool:
    return isinstance(notion, Client)


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            title = "".join(part.get("plain_text", "") for part in prop.get("title", []))
            return title.strip() or "（無題）"
    return "（無題）"


def fetch_block_children(block_id: str, *, recursive: bool = True) -> list[dict]:
    """Fetch every child block, following pagination and nesting."""
    notion = _get_notion()
    blocks: list[dict] = []
    cursor: str | None = None

    while True:
        response = _notion_call(
            "blocks.children.list",
            lambda: notion.blocks.children.list(
                block_id=block_id, start_cursor=cursor, page_size=100
            ),
            sdk_retries=_uses_sdk_retries(notion),
        )
        _throttle()
        blocks.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    if recursive:
        for block in blocks:
            # child_page is a separate page; it is walked as its own document.
            if block.get("has_children") and block.get("type") != "child_page":
                block["children"] = fetch_block_children(block["id"])

    return blocks


def fetch_database_rows(database_id: str, *, database: dict | None = None) -> tuple[str, list[dict]]:
    """Return a database's title and every row (as page objects).

    Since Notion API 2025-09-03 a database holds one or more data sources, and
    rows are queried per data source rather than per database.
    """
    notion = _get_notion()
    if database is None:
        database = _notion_call(
            "databases.retrieve",
            lambda: notion.databases.retrieve(database_id=database_id),
            sdk_retries=_uses_sdk_retries(notion),
        )
        _throttle()
    title = "".join(part.get("plain_text", "") for part in database.get("title", []))

    rows: list[dict] = []
    for source in database.get("data_sources", []):
        rows.extend(_query_data_source(source["id"]))
    return title, rows


def _query_data_source(data_source_id: str) -> list[dict]:
    notion = _get_notion()
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs = {"data_source_id": data_source_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = _notion_call(
            "data_sources.query",
            lambda: notion.data_sources.query(**kwargs),
            sdk_retries=_uses_sdk_retries(notion),
        )
        _throttle()
        rows.extend(item for item in response.get("results", []) if item.get("object") == "page")
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return rows


def search_shared() -> tuple[list[dict], list[str]]:
    """Everything connected to the integration: (page objects, database ids).

    A data source in the results stands for its database, so the database is
    walked whole (title and every data source).
    """
    notion = _get_notion()
    pages: list[dict] = []
    database_ids: list[str] = []
    cursor: str | None = None
    while True:
        kwargs = {"page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = _notion_call(
            "search",
            lambda: notion.search(**kwargs),
            sdk_retries=_uses_sdk_retries(notion),
        )
        _throttle()
        for item in response.get("results", []):
            if item.get("object") == "page":
                pages.append(item)
            elif item.get("object") == "data_source":
                parent = item.get("parent", {})
                database_ids.append(parent.get("database_id") or item["id"])
            elif item.get("object") == "database":
                database_ids.append(item["id"])
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return pages, database_ids


def _is_database_row(page: dict) -> bool:
    return page.get("parent", {}).get("type") in ("data_source_id", "database_id")


class _NotionAncestorResolver:
    """Resolve Notion parents once and test whether an object is excluded."""

    def __init__(self, notion, excluded: set[str]):
        self.notion = notion
        self.excluded = excluded
        self.objects: dict[tuple[str, str], dict] = {}
        self._inaccessible: dict[tuple[str, str], BaseException] = {}
        self._logged_inaccessible: set[tuple[str, str]] = set()

    def _parent_reference(self, parent: dict) -> tuple[str, str] | None:
        """Return the first page/database/data-source parent.

        A page can be nested inside a toggle, column, or synced block. Notion
        represents that relationship as ``block_id``, so resolve blocks until
        the owning page or database is reached.
        """
        current = parent
        visited_blocks: set[str] = set()
        while True:
            parent_type = current.get("type", "")
            for object_type, field in (
                ("page", "page_id"),
                ("database", "database_id"),
                ("data_source", "data_source_id"),
            ):
                if parent_type == field and current.get(field):
                    return object_type, current[field]
            if parent_type != "block_id" or not current.get("block_id"):
                return None

            block_id = current["block_id"]
            block_key = config.normalize_id(block_id)
            if block_key in visited_blocks:
                return None
            visited_blocks.add(block_key)
            block = self.load("block", block_id, ancestor=True)
            current = block.get("parent", {})

    def load(
        self,
        object_type: str,
        object_id: str,
        obj: dict | None = None,
        *,
        ancestor: bool = False,
    ) -> dict | None:
        key = (object_type, config.normalize_id(object_id))
        if obj is not None:
            self.objects.setdefault(key, obj)
        if key in self.objects:
            return self.objects[key]
        if key in self._inaccessible:
            if ancestor:
                self._log_inaccessible(key, self._inaccessible[key])
            raise self._inaccessible[key]

        try:
            if object_type == "page":
                obj = _notion_call(
                    "pages.retrieve",
                    lambda: self.notion.pages.retrieve(page_id=object_id),
                    sdk_retries=_uses_sdk_retries(self.notion),
                )
            elif object_type == "database":
                obj = _notion_call(
                    "databases.retrieve",
                    lambda: self.notion.databases.retrieve(database_id=object_id),
                    sdk_retries=_uses_sdk_retries(self.notion),
                )
            elif object_type == "data_source":
                retrieve = getattr(self.notion.data_sources, "retrieve", None)
                if retrieve is None:
                    return None
                obj = _notion_call(
                    "data_sources.retrieve",
                    lambda: retrieve(data_source_id=object_id),
                    sdk_retries=_uses_sdk_retries(self.notion),
                )
            elif object_type == "block":
                retrieve = getattr(self.notion.blocks, "retrieve", None)
                if retrieve is None:
                    return None
                obj = _notion_call(
                    "blocks.retrieve",
                    lambda: retrieve(block_id=object_id),
                    sdk_retries=_uses_sdk_retries(self.notion),
                )
            else:
                raise ValueError(f"unsupported Notion object type: {object_type}")
        except HTTPResponseError as exc:
            self._inaccessible[key] = exc
            if ancestor:
                self._log_inaccessible(key, exc)
            raise
        _throttle()
        self.objects[key] = obj
        if object_type == "database":
            for source in obj.get("data_sources", []):
                source_id = source.get("id")
                if source_id:
                    source_obj = dict(source)
                    source_obj.setdefault(
                        "parent", {"type": "database_id", "database_id": object_id}
                    )
                    self.objects.setdefault(
                        ("data_source", config.normalize_id(source_id)), source_obj
                    )
        return obj

    def _log_inaccessible(self, key: tuple[str, str], exc: BaseException) -> None:
        if key in self._logged_inaccessible:
            return
        self._logged_inaccessible.add(key)
        logger.info(
            "祖先 %s:%s を取得できないため、祖先による除外判定を打ち切ります: %s",
            key[0],
            key[1],
            exc,
        )

    def is_excluded(self, object_type: str, object_id: str, obj: dict | None = None) -> bool:
        current_type = object_type
        current_id = object_id
        current_obj = obj
        visited: set[tuple[str, str]] = set()

        while True:
            key = (current_type, config.normalize_id(current_id))
            if key[1] in self.excluded:
                return True
            if key in visited:
                return False
            visited.add(key)

            try:
                current_obj = self.load(current_type, current_id, current_obj, ancestor=True)
                if current_obj is None:
                    return False
                reference = self._parent_reference(current_obj.get("parent", {}))
            except _NOTION_READ_ERRORS:
                return False
            if reference is None:
                return False
            current_type, current_id = reference
            current_obj = None

    def database_title(self, page: dict) -> str:
        """Resolve the owning database title for a page reached by any path."""
        visited: set[tuple[str, str]] = set()
        try:
            reference = self._parent_reference(page.get("parent", {}))
            while reference is not None:
                object_type, object_id = reference
                key = (object_type, config.normalize_id(object_id))
                if key in visited:
                    return ""
                visited.add(key)
                obj = self.load(object_type, object_id, ancestor=True)
                if obj is None:
                    return ""
                if object_type == "database":
                    return "".join(part.get("plain_text", "") for part in obj.get("title", []))
                reference = self._parent_reference(obj.get("parent", {}))
        except _NOTION_READ_ERRORS:
            return ""
        return ""


def _find_nested(blocks: list[dict]) -> tuple[list[str], list[str]]:
    """Collect child page and child database ids at any depth.

    Dashboards put databases inside columns and callouts, so a top-level scan
    alone misses most of them.
    """
    pages: list[str] = []
    databases: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "child_page":
            pages.append(block["id"])
        elif btype == "child_database":
            databases.append(block["id"])
        if block.get("children"):
            nested_pages, nested_dbs = _find_nested(block["children"])
            pages.extend(nested_pages)
            databases.extend(nested_dbs)
    return pages, databases


def _block_cache_path(page_id: str) -> Path:
    return config.BLOCK_CACHE_DIR / f"{config.normalize_id(page_id)}.json"


def _load_block_cache(page_id: str, last_edited_time: str) -> list[dict] | None:
    path = _block_cache_path(page_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ブロックキャッシュを読み込めません (%s): %s", page_id, type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("last_edited_time") != last_edited_time:
        return None
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return None
    return blocks


def _write_block_cache(page_id: str, last_edited_time: str, blocks: list[dict]) -> None:
    path = _block_cache_path(page_id)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                {"last_edited_time": last_edited_time, "blocks": blocks},
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        # The cache is an optimization; a read-only cache directory must not
        # turn a successfully fetched page into a failed page.
        logger.warning("ブロックキャッシュを書き込めません (%s): %s", page_id, type(exc).__name__)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def discover_pages(
    root_ids: list[str],
    *,
    limit: int | None = None,
    use_block_cache: bool = True,
    fetch_blocks: bool = True,
) -> list[dict]:
    """Run Phase A discovery and return page metadata candidates.

    Token-only discovery uses the search results and every connected database
    row, so it never calls ``blocks.children.list``. Explicit-root discovery
    retains the small recursive walk needed to find nested pages and inline
    databases; those blocks are kept on the returned records for Phase B.
    """
    discover_pages.last_failed_roots = []
    discover_pages.last_failed_pages = []
    discover_pages.last_failed_page_titles = []
    discover_pages.last_discovered_page_count = 0
    discover_pages.last_phase_a_failure_count = 0
    notion = _get_notion()
    excluded = {config.normalize_id(pid) for pid in config.NOTION_EXCLUDE_PAGE_IDS}
    ancestors = _NotionAncestorResolver(notion, excluded)
    seen: set[str] = set()
    pages: list[dict] = []
    root_keys = {config.normalize_id(root_id) for root_id in root_ids}
    failed_database_keys: set[str] = set()
    failed_root_keys: set[str] = set()
    # Each item: ("id", page_or_database_id, None) or ("row", row_id, (row, db_title)).
    queue: list[tuple[str, str, tuple[dict, str] | None]] = []

    def mark_page_failed(page_id: str, title: str = "（タイトル不明）") -> None:
        normalized_id = config.normalize_id(page_id)
        failed_ids = {
            config.normalize_id(failed_id) for failed_id in discover_pages.last_failed_pages
        }
        if normalized_id in failed_ids:
            return
        discover_pages.last_failed_pages.append(page_id)
        discover_pages.last_failed_page_titles.append(title or "（タイトル不明）")

    def report_page_failure(
        page_id: str, title: str, exc: BaseException, *, count_as_discovered: bool = False
    ) -> None:
        if count_as_discovered:
            discover_pages.last_discovered_page_count += 1
            discover_pages.last_phase_a_failure_count += 1
        mark_page_failed(page_id, title)
        logger.error(
            "ページを取得できません (id=%s, title=%s, status=%s): %s",
            page_id,
            title or "（タイトル不明）",
            _error_status(exc) or type(exc).__name__,
            type(exc).__name__,
        )

    def mark_root_failed(item_id: str) -> None:
        key = config.normalize_id(item_id)
        if key in root_keys and key not in failed_root_keys:
            failed_root_keys.add(key)
            discover_pages.last_failed_roots.append(item_id)

    def enqueue_rows(db_title: str, rows: list[dict]) -> None:
        logger.info("database %s: %d rows", db_title, len(rows))
        queue.extend(("row", row["id"], (row, db_title)) for row in rows)

    def enqueue_database(database_id: str, *, report_failure: bool = True) -> bool:
        db_key = config.normalize_id(database_id)
        if db_key in seen:
            return db_key not in failed_database_keys
        seen.add(db_key)
        try:
            database = ancestors.load("database", database_id)
            if ancestors.is_excluded("database", database_id, database):
                logger.info("excluded by config: %s", database_id)
                return True
            db_title, rows = fetch_database_rows(database_id, database=database)
        except _NOTION_READ_ERRORS as exc:
            # Linked views of databases outside the connected pages land here.
            failed_database_keys.add(db_key)
            if db_key in root_keys:
                mark_root_failed(database_id)
            if report_failure:
                logger.error("データベースを取得できません (%s): %s", database_id, exc)
            return False
        enqueue_rows(db_title, rows)
        return True

    def append_metadata(page: dict, database_title: str = "") -> bool:
        """Apply the common Phase A exclusion and build a metadata record."""
        page_id = page.get("id")
        if not page_id:
            return False
        if ancestors.is_excluded("page", page_id, page):
            logger.info("excluded by config: %s", page_id)
            return False
        pages.append(
            {
                "id": page_id,
                "title": _page_title(page),
                "url": page.get("url", ""),
                "last_edited": page.get("last_edited_time", ""),
                "database": database_title,
                "properties": page.get("properties", {}),
            }
        )
        discover_pages.last_discovered_page_count += 1
        return True

    def resolve_search_page(search_result: dict) -> dict:
        """Use the complete search object, with a fake/partial-result fallback."""
        page_id = search_result["id"]
        if search_result.get("properties") and search_result.get("last_edited_time"):
            return search_result
        return ancestors.load("page", page_id) or search_result

    def discover_from_search() -> list[dict]:
        search_results, shared_databases = search_shared()
        root_keys.update(config.normalize_id(page["id"]) for page in search_results)
        root_keys.update(config.normalize_id(database_id) for database_id in shared_databases)
        logger.info(
            "search: %d pages, %d databases connected",
            len(search_results),
            len(shared_databases),
        )
        for database_id in shared_databases:
            if not enqueue_database(database_id, report_failure=False):
                mark_root_failed(database_id)

        page_seen: set[str] = set()
        # Database rows are already queued above. Search can return the same
        # rows, so append them after the database query and deduplicate by ID.
        while queue:
            kind, item_id, payload = queue.pop(0)
            if kind != "row":
                continue
            key = config.normalize_id(item_id)
            if key in page_seen:
                continue
            page, database_title = payload
            page_seen.add(key)
            append_metadata(page, database_title)

        for search_result in search_results:
            page_id = search_result["id"]
            key = config.normalize_id(page_id)
            if key in page_seen:
                continue
            try:
                page = resolve_search_page(search_result)
            except _NOTION_READ_ERRORS as exc:
                if key in root_keys:
                    mark_root_failed(page_id)
                report_page_failure(page_id, "（タイトル不明）", exc, count_as_discovered=True)
                continue
            page_seen.add(key)
            append_metadata(page, ancestors.database_title(page))
        return pages

    if root_ids:
        queue.extend(("id", root_id, None) for root_id in root_ids)
    else:
        return discover_from_search()

    while queue:
        kind, item_id, payload = queue.pop(0)
        key = config.normalize_id(item_id)
        if key in seen:
            continue

        if kind == "row":
            page, database_title = payload
            if ancestors.is_excluded("page", item_id, page):
                logger.info("excluded by config: %s", item_id)
                continue
        else:
            try:
                page = ancestors.load("page", item_id)
            except _NOTION_READ_ERRORS as page_exc:
                # A root id may point at a database rather than a page.
                if not enqueue_database(item_id, report_failure=False):
                    if key in root_keys:
                        mark_root_failed(item_id)
                    if _is_access_error(page_exc):
                        if key in root_keys:
                            logger.error(
                                "ページを取得できません (%s): %s / Notion で「接続を追加」をしたか確認してください",
                                item_id,
                                page_exc,
                            )
                        else:
                            logger.info("ページを取得できません (%s): %s", item_id, page_exc)
                    else:
                        report_page_failure(
                            item_id,
                            "（タイトル不明）",
                            page_exc,
                            count_as_discovered=True,
                        )
                continue
            database_title = ancestors.database_title(page)

        seen.add(key)
        if ancestors.is_excluded("page", item_id, page):
            logger.info("excluded by config: %s", item_id)
            continue

        page_title = _page_title(page)
        last_edited_time = page.get("last_edited_time", "")
        blocks: list[dict] = []
        if fetch_blocks:
            cached_blocks = (
                _load_block_cache(page["id"], last_edited_time) if use_block_cache else None
            )
            if cached_blocks is not None:
                blocks = cached_blocks
                logger.info("ブロックキャッシュを再利用: %s", page["id"])
            else:
                try:
                    blocks = fetch_block_children(page["id"])
                except _NOTION_READ_ERRORS as exc:
                    report_page_failure(page["id"], page_title, exc, count_as_discovered=True)
                    mark_root_failed(page["id"])
                    continue
                if use_block_cache:
                    _write_block_cache(page["id"], last_edited_time, blocks)
        record = {
            "id": page["id"],
            "title": page_title,
            "url": page.get("url", ""),
            "last_edited": page.get("last_edited_time", ""),
            "database": database_title,
            "properties": page.get("properties", {}),
            "blocks": blocks,
        }
        pages.append(record)
        discover_pages.last_discovered_page_count += 1

        if fetch_blocks:
            nested_pages, nested_databases = _find_nested(blocks)
            queue.extend(("id", page_id, None) for page_id in nested_pages)
            for database_id in nested_databases:
                enqueue_database(database_id)

    return pages


def _caption_factory(page_title: str, blocks: list[dict], *, skip_images: bool):
    """Build the image-caption callback handed to notion_md."""
    headings = _heading_lookup(blocks)

    def caption_for(block: dict) -> str | None:
        if skip_images:
            return None
        image = block.get("image", {})
        source = image.get(image.get("type", ""), {})
        url = source.get("url", "")
        if not url:
            return None
        return vision_client.describe_image(
            block["id"],
            url,
            page_title=page_title,
            nearest_heading=headings.get(block["id"], ""),
            notion_caption=notion_md.rich_text_to_md(image.get("caption", [])),
        )

    return caption_for


def _heading_lookup(blocks: list[dict]) -> dict[str, str]:
    """Map each image block id to the heading text that precedes it."""
    mapping: dict[str, str] = {}

    def walk(items: list[dict], current: str) -> str:
        for block in items:
            btype = block.get("type", "")
            if btype in ("heading_1", "heading_2", "heading_3"):
                current = "".join(
                    part.get("plain_text", "")
                    for part in block.get(btype, {}).get("rich_text", [])
                )
            elif btype == "image":
                mapping[block["id"]] = current
            if block.get("children"):
                current = walk(block["children"], current)
        return current

    walk(blocks, "")
    return mapping


def _load_previous(page_id: str) -> list[dict]:
    path = _cache_path(page_id)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _cache_path(page_id: str) -> Path:
    return config.CLEAN_DIR / f"{config.normalize_id(page_id)}.jsonl"


def process_page(page: dict, *, skip_images: bool) -> list[dict]:
    """Fetch, convert, chunk and return records for one page."""
    blocks = page.get("blocks")
    if blocks is None:
        # Phase B normally supplies blocks from _load_phase_b_blocks. Keeping
        # the fallback here makes direct callers reuse the same cache too.
        blocks = _load_phase_b_blocks(page, use_block_cache=True)
    title, title_redacted = redact.redact(page["title"])
    caption_for = _caption_factory(title, blocks, skip_images=skip_images)
    markdown = notion_md.blocks_to_markdown(
        blocks,
        page_id=page["id"],
        title=title,
        caption_for=caption_for,
        properties=page.get("properties"),
    )
    markdown, body_redacted = redact.redact(markdown)
    redacted = title_redacted + body_redacted
    if redacted:
        logger.info("認証情報を伏字にしました: %d 箇所 (%s)", redacted, title)
    chunks = chunking.chunk_markdown(markdown)

    records = []
    for seq, chunk in enumerate(chunks):
        image_links = [
            line.split("画像リンク:", 1)[1].strip()
            for line in chunk["text"].splitlines()
            if "画像リンク:" in line
        ]
        records.append(
            {
                "id": f"{page['id']}-{seq}",
                "text": chunk["text"],
                "title": title,
                "url": page["url"],
                "last_edited": page["last_edited"],
                "source": "notion",
                "has_image": bool(image_links),
                "image_links": ",".join(image_links),
                "heading_path": " > ".join(chunk["heading_path"]),
                "chunker_version": config.CHUNKER_VERSION,
            }
        )
    return records


def write_records(page_id: str, records: list[dict]) -> None:
    config.CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(page_id)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cached_records(page: dict, *, force: bool) -> list[dict] | None:
    if force:
        return None
    records = _load_previous(page["id"])
    if (
        records
        and all(record.get("last_edited") == page["last_edited"] for record in records)
        and all(record.get("chunker_version") == config.CHUNKER_VERSION for record in records)
    ):
        return records
    return None


def _load_phase_b_blocks(page: dict, *, use_block_cache: bool) -> list[dict]:
    """Load content for one metadata candidate without discovering siblings."""
    # Root-mode discovery already fetched these blocks to find nested objects.
    if "blocks" in page:
        return page["blocks"]

    if use_block_cache:
        blocks = _load_block_cache(page["id"], page["last_edited"])
        if blocks is not None:
            logger.info("ブロックキャッシュを再利用: %s", page["id"])
            return blocks

    blocks = fetch_block_children(page["id"])
    if use_block_cache:
        _write_block_cache(page["id"], page["last_edited"], blocks)
    return blocks


def _write_selected(
    page_ids: list[str],
    total_chunks: int,
    budget: int,
    failed_page_ids: list[str] | None = None,
) -> None:
    path = config.DATA_DIR / "selected.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "budget": budget,
                "total_chunks": total_chunks,
                "page_ids": page_ids,
                "failed_page_ids": failed_page_ids or [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _print_dry_run(pages: list[dict], ordered: list[dict]) -> None:
    """Print the Phase A scope without requiring any page content."""
    tier_counts = {tier: 0 for tier in (1, 2, 3, 4)}
    excluded = 0
    for page in pages:
        tier = page.get("tier")
        if tier is None:
            excluded += 1
        else:
            tier_counts[tier] += 1
    for tier in (1, 2, 3, 4):
        print(f"Tier {tier}: {tier_counts[tier]} ページ")
    print(f"キーワード除外: {excluded} ページ")

    database_counts: dict[str, int] = {}
    for page in pages:
        database = page.get("database", "")
        if database:
            database_counts[database] = database_counts.get(database, 0) + 1
    if database_counts:
        print("データベース別候補（上位15）:")
        for database, count in sorted(
            database_counts.items(), key=lambda item: (-item[1], item[0])
        )[:15]:
            print(f"  {database}: {count} ページ")


def _print_summary(
    pages: list[dict],
    selected: list[dict],
    chunk_counts: dict[str, int],
    total_chunks: int,
    budget: int,
    *,
    pages_attempted: int = 0,
    pages_fetched: int = 0,
    failed_page_ids: list[str] | None = None,
    eligible_page_count: int | None = None,
) -> None:
    selected_ids = {page["id"] for page in selected}
    for tier in (1, 2, 3, 4):
        tier_pages = [page for page in pages if page.get("tier") == tier]
        tier_selected = [page for page in tier_pages if page["id"] in selected_ids]
        tier_chunks = sum(chunk_counts.get(page["id"], 0) for page in tier_selected)
        print(
            f"Tier {tier}: {len(tier_selected)} ページ / {tier_chunks} チャンク "
            f"（選択外: {len(tier_pages) - len(tier_selected)} ページ）"
        )
    excluded = sum(page.get("tier") is None for page in pages)
    budget_text = "無制限" if budget == 0 else f"{budget} チャンク"
    print(
        f"合計: {len(selected)} ページ / {total_chunks} チャンク "
        f"（予算: {budget_text}）"
    )
    print(f"キーワード除外: {excluded} ページ")
    print(f"ページ取得: {pages_fetched} 件 / 処理試行: {pages_attempted} 件")
    unfetched = eligible_page_count if eligible_page_count is not None else len(pages)
    print(f"未取得: {max(0, unfetched - pages_attempted)} ページ")
    print(f"取得失敗: {len(failed_page_ids or [])} ページ")

    database_chunks: dict[str, int] = {}
    for page in selected:
        database = page.get("database", "")
        if database:
            database_chunks[database] = database_chunks.get(database, 0) + chunk_counts.get(
                page["id"], 0
            )
    if database_chunks:
        print("データベース別選択チャンク（上位15）:")
        for database, count in sorted(
            database_chunks.items(), key=lambda item: (-item[1], item[0])
        )[:15]:
            print(f"  {database}: {count} チャンク")


def _discover_for_main(*, dry_run: bool, use_block_cache: bool) -> list[dict]:
    """Call discovery while keeping small test doubles source-compatible."""
    kwargs: dict[str, object] = {}
    try:
        parameters = inspect.signature(discover_pages).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "use_block_cache" in parameters and not use_block_cache:
        kwargs["use_block_cache"] = False
    if "fetch_blocks" in parameters:
        kwargs["fetch_blocks"] = not dry_run
    return discover_pages(config.NOTION_ROOT_PAGE_IDS, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Notion を取り込み data/clean/*.jsonl を作る"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ページ一覧を表示するだけ。OpenAI 課金ゼロ。取り込み範囲の確認に使う",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="画像キャプションを行わない（VLM 課金ゼロ）",
    )
    parser.add_argument("--limit", type=int, default=None, help="先頭 N ページのみ処理")
    parser.add_argument(
        "--force", action="store_true", help="last_edited を無視して全ページ再処理"
    )
    parser.add_argument(
        "--no-block-cache",
        action="store_true",
        help="再帰ブロックキャッシュを使わず Notion から再取得する",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=config.CHUNK_BUDGET,
        help="選択する最大チャンク数。0 は無制限（既定: config.CHUNK_BUDGET）",
    )
    args = parser.parse_args()
    if args.budget < 0:
        parser.error("--budget は 0 以上で指定してください")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit は 0 以上で指定してください")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.ensure_dirs()

    if config.NOTION_ROOT_PAGE_IDS:
        print(f"取り込み範囲: NOTION_ROOT_PAGE_IDS の {len(config.NOTION_ROOT_PAGE_IDS)} 件の配下")
    else:
        print("取り込み範囲: このトークンに接続されているもの全部（NOTION_ROOT_PAGE_IDS 未設定）")

    pages = _discover_for_main(
        dry_run=args.dry_run,
        use_block_cache=not args.no_block_cache,
    )
    print(f"対象ページ: {len(pages)} 件")
    ordered = selection.order_pages(pages)
    failed_roots = list(getattr(discover_pages, "last_failed_roots", []))
    failed_page_ids = list(getattr(discover_pages, "last_failed_pages", []))
    failed_page_titles = list(getattr(discover_pages, "last_failed_page_titles", []))
    discovered_page_count = int(
        getattr(discover_pages, "last_discovered_page_count", len(pages) + len(failed_page_ids))
    )
    print(f"取得失敗: {len(failed_page_ids)} ページ（前回の索引を残します）")
    for title in failed_page_titles:
        print(f"  {title}")

    if failed_roots:
        print(
            "ルートページの取得に失敗したため、data/selected.json は更新しません。"
            f"（失敗: {len(failed_roots)} 件）",
            file=sys.stderr,
        )
        return 1
    if not pages:
        print(
            "取り込み対象ページが 0 件のため、data/selected.json は更新しません。",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        _print_dry_run(pages, ordered)
        print("data/selected.json は --dry-run のため更新しません")
        return 0

    chunk_counts: dict[str, int] = {}
    cached_pages = 0
    selector = selection.IncrementalBudgetSelector(args.budget)
    consecutive_non_fitting = 0
    phase_b_failed_ids: list[str] = []
    phase_b_attempts = 0
    pages_fetched = 0

    candidates = ordered if args.limit is None else ordered[: args.limit]

    def record_failure(page: dict, exc: BaseException) -> None:
        normalized_id = config.normalize_id(page["id"])
        if normalized_id not in {
            config.normalize_id(failed_id) for failed_id in failed_page_ids
        }:
            failed_page_ids.append(page["id"])
            failed_page_titles.append(page.get("title", "（タイトル不明）"))
        if normalized_id not in {
            config.normalize_id(failed_id) for failed_id in phase_b_failed_ids
        }:
            phase_b_failed_ids.append(page["id"])
        logger.error(
            "ページを処理できません (id=%s, title=%s, status=%s): %s",
            page["id"],
            page.get("title", "（タイトル不明）"),
            _error_status(exc) or type(exc).__name__,
            type(exc).__name__,
        )

    for page in candidates:
        phase_b_attempts += 1
        try:
            records = _cached_records(page, force=args.force)
            if records is not None:
                cached_pages += 1
            else:
                blocks = _load_phase_b_blocks(
                    page,
                    use_block_cache=not args.no_block_cache,
                )
                content_page = dict(page)
                content_page["blocks"] = blocks
                records = process_page(content_page, skip_images=args.no_images)
                write_records(page["id"], records)
                print(f"  {page['title']}: {len(records)} チャンク")
            pages_fetched += 1
        except Exception as exc:
            record_failure(page, exc)
            if config.normalize_id(page["id"]) in {
                config.normalize_id(root_id) for root_id in config.NOTION_ROOT_PAGE_IDS
            }:
                failed_roots.append(page["id"])
            if phase_b_attempts % 50 == 0:
                logger.info(
                    "進捗: %d ページ処理 / 選択 %d ページ / %d チャンク / 予算 %d",
                    phase_b_attempts,
                    len(selector.selected_ids),
                    selector.total,
                    args.budget,
                )
            continue

        chunk_count = len(records)
        chunk_counts[page["id"]] = chunk_count
        if selector.try_add(page["id"], chunk_count):
            consecutive_non_fitting = 0
        else:
            consecutive_non_fitting += 1

        if phase_b_attempts % 50 == 0:
            logger.info(
                "進捗: %d ページ処理 / 選択 %d ページ / %d チャンク / 予算 %d",
                phase_b_attempts,
                len(selector.selected_ids),
                selector.total,
                args.budget,
            )
        if args.budget and selector.total >= args.budget:
            break
        if consecutive_non_fitting >= config.MAX_CONSECUTIVE_NON_FITTING_PAGES:
            logger.info(
                "予算に収まらないページが%d件連続したため、Phase Bを終了します",
                config.MAX_CONSECUTIVE_NON_FITTING_PAGES,
            )
            break

    selected_ids = set(selector.selected_ids)
    selected = [page for page in ordered if page["id"] in selected_ids]
    total_chunks = selector.total
    _print_summary(
        pages,
        selected,
        chunk_counts,
        total_chunks,
        args.budget,
        pages_attempted=phase_b_attempts,
        pages_fetched=pages_fetched,
        failed_page_ids=failed_page_ids,
        eligible_page_count=len(ordered),
    )
    print(f"キャッシュ再利用: {cached_pages} ページ")

    # The real discovery function records Phase A failures separately. Keep a
    # fallback for small test doubles that still expose the old attributes.
    if phase_b_failed_ids or hasattr(discover_pages, "last_phase_a_failure_count"):
        failure_count = len(phase_b_failed_ids)
        failure_denominator = phase_b_attempts
    else:
        failure_count = len(failed_page_ids)
        failure_denominator = discovered_page_count
    if (
        failure_denominator > 0
        and failure_count * 100 > failure_denominator * 5
    ):
        print(
            "取得失敗が発見ページの5%を超えたため、data/selected.json は更新しません。",
            file=sys.stderr,
        )
        return 1
    if failed_roots:
        print(
            "ルートページの取得に失敗したため、data/selected.json は更新しません。"
            f"（失敗: {len(failed_roots)} 件）",
            file=sys.stderr,
        )
        return 1
    if args.limit is not None:
        print("data/selected.json は --limit 指定のため更新をスキップしました")
    else:
        _write_selected(
            [page["id"] for page in selected],
            total_chunks,
            args.budget,
            failed_page_ids=failed_page_ids,
        )
        print(f"完了: data/selected.json に {len(selected)} ページを記録しました")
    print("次は: uv run python index_build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
