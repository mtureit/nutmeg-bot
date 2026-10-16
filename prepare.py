"""Phase 1-1: Notion -> Markdown -> chunks -> JSONL (PLAN.md 10).

Page discovery starts from NOTION_ROOT_PAGE_IDS and walks down. The search API
is deliberately not used: its scope is "everything shared with the integration",
which drifts as people connect new pages. An explicit root list keeps the ingest
boundary visible outside the code (PLAN.md 10.0, 17.1).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from notion_client import Client
from notion_client.errors import APIResponseError

import chunking
import config
import notion_md
import vision_client

logger = logging.getLogger(__name__)

_notion: Client | None = None


def _get_notion() -> Client:
    global _notion
    if _notion is None:
        config.require("NOTION_TOKEN")
        _notion = Client(auth=config.NOTION_TOKEN)
    return _notion


def _throttle() -> None:
    """Notion allows roughly 3 requests/second on average."""
    time.sleep(config.NOTION_SLEEP_SEC)


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
        response = notion.blocks.children.list(
            block_id=block_id, start_cursor=cursor, page_size=100
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


def fetch_database_rows(database_id: str) -> tuple[str, list[dict]]:
    """Return a database's title and every row (as page objects).

    Since Notion API 2025-09-03 a database holds one or more data sources, and
    rows are queried per data source rather than per database.
    """
    notion = _get_notion()
    database = notion.databases.retrieve(database_id=database_id)
    _throttle()
    title = "".join(part.get("plain_text", "") for part in database.get("title", []))

    rows: list[dict] = []
    for source in database.get("data_sources", []):
        cursor: str | None = None
        while True:
            kwargs = {"data_source_id": source["id"], "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = notion.data_sources.query(**kwargs)
            _throttle()
            rows.extend(
                item for item in response.get("results", []) if item.get("object") == "page"
            )
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
    return title, rows


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


def discover_pages(root_ids: list[str], *, limit: int | None = None) -> list[dict]:
    """Walk down from the root pages, collecting pages and database rows.

    A root id may point at a page or at a database. Each page's blocks are
    fetched here once (to find nested pages and databases) and kept on the
    record, so process_page does not fetch them again.
    """
    notion = _get_notion()
    excluded = {config.normalize_id(pid) for pid in config.NOTION_EXCLUDE_PAGE_IDS}
    seen: set[str] = set()
    pages: list[dict] = []
    # Each item: ("id", page_or_database_id, None) or ("row", row_id, (row, db_title)).
    queue: list[tuple[str, str, tuple[dict, str] | None]] = [
        ("id", root_id, None) for root_id in root_ids
    ]

    def enqueue_rows(db_title: str, rows: list[dict]) -> None:
        logger.info("database %s: %d rows", db_title, len(rows))
        queue.extend(("row", row["id"], (row, db_title)) for row in rows)

    def enqueue_database(database_id: str) -> None:
        db_key = config.normalize_id(database_id)
        if db_key in seen:
            return
        seen.add(db_key)
        if db_key in excluded:
            logger.info("excluded by config: %s", database_id)
            return
        try:
            db_title, rows = fetch_database_rows(database_id)
        except APIResponseError as exc:
            # Linked views of databases outside the connected pages land here.
            logger.error("データベースを取得できません (%s): %s", database_id, exc)
            return
        enqueue_rows(db_title, rows)

    while queue:
        if limit is not None and len(pages) >= limit:
            break
        kind, item_id, payload = queue.pop(0)
        key = config.normalize_id(item_id)
        if key in seen:
            continue

        if kind == "row":
            page, database_title = payload
        else:
            try:
                page = notion.pages.retrieve(page_id=item_id)
                _throttle()
            except APIResponseError as page_exc:
                # A root id may point at a database rather than a page.
                try:
                    db_title, rows = fetch_database_rows(item_id)
                except APIResponseError:
                    logger.error(
                        "ページを取得できません (%s): %s / Notion 側で「接続を追加」をしたか確認してください",
                        item_id,
                        page_exc,
                    )
                    continue
                seen.add(key)
                if key in excluded:
                    logger.info("excluded by config: %s", item_id)
                    continue
                enqueue_rows(db_title, rows)
                continue
            database_title = ""

        seen.add(key)
        if key in excluded:
            logger.info("excluded by config: %s", item_id)
            continue

        blocks = fetch_block_children(page["id"])
        pages.append(
            {
                "id": page["id"],
                "title": _page_title(page),
                "url": page.get("url", ""),
                "last_edited": page.get("last_edited_time", ""),
                "database": database_title,
                "properties": page.get("properties", {}) if database_title else {},
                "blocks": blocks,
            }
        )

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
    path = config.CLEAN_DIR / f"{config.normalize_id(page_id)}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def process_page(page: dict, *, skip_images: bool) -> list[dict]:
    """Fetch, convert, chunk and return records for one page."""
    blocks = page.get("blocks")
    if blocks is None:
        blocks = fetch_block_children(page["id"])
    caption_for = _caption_factory(page["title"], blocks, skip_images=skip_images)
    markdown = notion_md.blocks_to_markdown(
        blocks,
        page_id=page["id"],
        title=page["title"],
        caption_for=caption_for,
        properties=page.get("properties"),
    )
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
                "title": page["title"],
                "url": page["url"],
                "last_edited": page["last_edited"],
                "source": "notion",
                "has_image": bool(image_links),
                "image_links": ",".join(image_links),
                "heading_path": " > ".join(chunk["heading_path"]),
            }
        )
    return records


def write_records(page_id: str, records: list[dict]) -> None:
    config.CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CLEAN_DIR / f"{config.normalize_id(page_id)}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.ensure_dirs()

    if not config.NOTION_ROOT_PAGE_IDS:
        print(
            "NOTION_ROOT_PAGE_IDS が未設定です。.env に取り込み起点のページ ID を"
            "カンマ区切りで設定してください（PLAN.md §10.0）",
            file=sys.stderr,
        )
        return 1

    pages = discover_pages(config.NOTION_ROOT_PAGE_IDS, limit=args.limit)
    print(f"対象ページ: {len(pages)} 件")

    if args.dry_run:
        # Eyeballing this list before indexing is the check in PLAN.md 17.8.
        for page in pages:
            where = f"[{page['database']}] " if page.get("database") else ""
            print(f"  {page['last_edited'][:10]}  {where}{page['title']}  {page['url']}")
        return 0

    total_chunks = 0
    skipped = 0
    for page in pages:
        previous = _load_previous(page["id"])
        if not args.force and previous and previous[0].get("last_edited") == page["last_edited"]:
            skipped += 1
            total_chunks += len(previous)
            continue

        records = process_page(page, skip_images=args.no_images)
        write_records(page["id"], records)
        total_chunks += len(records)
        print(f"  {page['title']}: {len(records)} チャンク")

    print(f"完了: {total_chunks} チャンク（未更新でスキップ: {skipped} ページ）")
    print("次は: uv run python index_build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
