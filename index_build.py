"""Phase 1-2: JSONL -> embeddings -> Chroma upsert (PLAN.md 8).

Chunk ids are deterministic ({page_id}-{seq}) so re-ingesting overwrites instead
of duplicating. Before writing anything the index metadata is compared against
config: an embedding model or dimension mismatch between ingest time and query
time silently ruins retrieval, so it aborts instead.
"""

from __future__ import annotations

import argparse
import json
import sys

import chromadb
import httpx

import config
import embedder
import redact
import vectorize_client


class VectorizeCapacityError(RuntimeError):
    """Raised before embedding when the free-tier dimension limit is exceeded."""


class VectorizeSelectionError(RuntimeError):
    """Raised when a selection is empty or would shrink the index unexpectedly."""


def _selection_metadata() -> tuple[set[str], set[str]] | None:
    path = config.DATA_DIR / "selected.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"data/selected.json を読み込めません: {exc}") from exc
    page_ids = payload.get("page_ids") if isinstance(payload, dict) else None
    if not isinstance(page_ids, list) or not all(isinstance(page_id, str) for page_id in page_ids):
        raise RuntimeError("data/selected.json の page_ids が不正です")
    failed_page_ids = payload.get("failed_page_ids", [])
    if not isinstance(failed_page_ids, list) or not all(
        isinstance(page_id, str) for page_id in failed_page_ids
    ):
        raise RuntimeError("data/selected.json の failed_page_ids が不正です")
    return (
        {config.normalize_id(page_id) for page_id in page_ids},
        {config.normalize_id(page_id) for page_id in failed_page_ids},
    )


def _selected_page_ids() -> set[str] | None:
    metadata = _selection_metadata()
    return None if metadata is None else metadata[0]


def _failed_page_ids() -> set[str]:
    metadata = _selection_metadata()
    return set() if metadata is None else metadata[1]


def _load_records() -> list[dict]:
    records: list[dict] = []
    if not config.CLEAN_DIR.exists():
        return records
    selected = _selected_page_ids()
    skipped_files = 0
    for path in sorted(config.CLEAN_DIR.glob("*.jsonl")):
        if selected is not None and config.normalize_id(path.stem) not in selected:
            skipped_files += 1
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                redact.redact_record(record)
                records.append(record)
    if selected is not None:
        print(f"selected.json にない JSONL を {skipped_files} 件スキップしました")
    return records


def embed_text(record: dict) -> str:
    """Text sent to the embedding model: the chunk prefixed with where it sits.

    A chunk split off by heading does not contain the page title, so a question
    naming the page ("クイズアプリのデプロイ") would miss the chunks that hold
    the actual steps and match only the near-empty first chunk. Legacy records
    still use the title and heading prefix here; current records carry their
    compact title/tag header in the stored chunk text.
    """
    # Records written by prepare.py already contain the compact title/tag
    # header. Keep legacy records (without the tags field) source-compatible
    # with the old title/heading prefix used by existing local indexes.
    if "tags" in record:
        title = str(record.get("title", ""))
        text = record.get("text", "")
        title_line = f"# {title}"
        if text.startswith(title_line) or text.startswith(f"[{title}]"):
            return text
        header = f"[{title}]"
        if record.get("tags"):
            header += f" タグ: {record['tags']}"
        return f"{header}\n\n{text}"

    header = [record.get("title", "")]
    if record.get("heading_path"):
        header.append(record["heading_path"])
    header = [part for part in header if part]
    if not header:
        return record["text"]
    return " > ".join(header) + "\n\n" + record["text"]


def _chunk_belongs_to_page(chunk_id: str, page_id: str) -> bool:
    """Match both dashed and normalized forms of the deterministic chunk id."""
    if chunk_id == page_id or chunk_id.startswith(f"{page_id}-"):
        return True
    normalized_chunk = config.normalize_id(chunk_id)
    normalized_page = config.normalize_id(page_id)
    suffix = normalized_chunk[len(normalized_page) :]
    return normalized_chunk.startswith(normalized_page) and suffix.isdigit()


def _read_index_meta() -> dict | None:
    if not config.INDEX_META_PATH.exists():
        return None
    try:
        return json.loads(config.INDEX_META_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_index_meta() -> None:
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    config.INDEX_META_PATH.write_text(
        json.dumps(
            {"embed_model": config.EMBED_MODEL, "embed_dim": config.EMBED_DIM},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def check_index_meta(*, reset: bool) -> bool:
    """Return False when the existing index was built with a different model."""
    meta = _read_index_meta()
    if meta is None or reset:
        return True
    if (
        meta.get("embed_model") != config.EMBED_MODEL
        or meta.get("embed_dim") != config.EMBED_DIM
    ):
        print(
            "既存インデックスと config が食い違っています。\n"
            f"  索引: {meta.get('embed_model')} / {meta.get('embed_dim')} 次元\n"
            f"  現在: {config.EMBED_MODEL} / {config.EMBED_DIM} 次元\n"
            "埋め込みモデルを変えた場合は全再索引が必要です: "
            "uv run python index_build.py --reset",
            file=sys.stderr,
        )
        return False
    return True


def _ensure_vectorize_capacity(record_count: int) -> None:
    max_records = config.FREE_TIER_MAX_VECTORS
    if record_count > max_records:
        raise VectorizeCapacityError(
            f"Vectorize の無料枠（保存可能な次元数 {config.FREE_TIER_STORED_DIMS:,}）を超えるため中止します: "
            f"{record_count} チャンク > 最大 {max_records} チャンク（{config.EMBED_DIM} 次元）。"
            "prepare.py の --budget を下げてください"
        )


def build_vectorize(records: list[dict], *, allow_shrink: bool = False) -> int:
    """Embed every record and upsert into Cloudflare Vectorize, then drop stale ids.

    Every run re-sends all vectors, so an embedding model change is applied in
    full without a separate reset step.
    """
    _ensure_vectorize_capacity(len(records))
    previous = vectorize_client.load_manifest()
    current = {record["id"] for record in records}
    failed_page_ids = _failed_page_ids()
    if not current:
        raise VectorizeSelectionError(
            "選択されたチャンクが 0 件のため、Vectorize の更新を中止します。"
            " prepare.py を --limit なしで実行してください"
        )

    stale = vectorize_client.stale_ids(previous, current)
    protected_stale = {
        chunk_id
        for chunk_id in stale
        if any(_chunk_belongs_to_page(chunk_id, page_id) for page_id in failed_page_ids)
    }
    deletable_stale = sorted(set(stale) - protected_stale)
    if previous and len(deletable_stale) * 100 > len(previous) * 30 and not allow_shrink:
        raise VectorizeSelectionError(
            "Vectorize の更新を中止します: "
            f"以前 {len(previous)} チャンク中、古い {len(deletable_stale)} チャンクを削除予定です（30%超）。"
            "意図した縮小なら --allow-shrink を指定してください"
        )

    vectorize_client.require_credentials()

    print(
        f"{len(records)} チャンクを埋め込み、Vectorize '{config.VECTORIZE_INDEX}' に送ります"
        f"（{config.EMBED_MODEL}）"
    )
    with httpx.Client(timeout=60) as client:
        if deletable_stale:
            vectorize_client.delete_ids(deletable_stale, client=client)
            print(f"  消えたチャンクを {len(deletable_stale)} 件削除しました")
        if protected_stale:
            print(f"  取得失敗ページの既存ベクトルを {len(protected_stale)} 件保持しました")

        for start in range(0, len(records), config.EMBED_BATCH_SIZE):
            batch = records[start : start + config.EMBED_BATCH_SIZE]
            vectors = embedder.embed([embed_text(record) for record in batch])
            vectorize_client.upsert(batch, vectors, client=client)
            print(f"  {min(start + len(batch), len(records))}/{len(records)}")

    # Keep protected ids in the manifest so a second run cannot mistake them
    # for ordinary stale ids before the page has been fetched successfully.
    vectorize_client.save_manifest(current | protected_stale)
    print(
        "完了: Vectorize への反映は非同期です。数十秒後から Worker の検索に効きます\n"
        "確認: cd worker && bunx wrangler vectorize info " + config.VECTORIZE_INDEX
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JSONL を埋め込み、索引に upsert する")
    parser.add_argument(
        "--reset", action="store_true", help="（chroma のみ）コレクションを作り直して全再索引する"
    )
    parser.add_argument(
        "--target",
        choices=("chroma", "vectorize"),
        default="chroma",
        help="書き込み先。chroma=ローカル（bot.py 用）、vectorize=Cloudflare（Worker 用）",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Vectorize で以前のマニフェストの30%超を削除する更新を許可",
    )
    args = parser.parse_args()

    config.ensure_dirs()

    records = _load_records()
    selected_exists = (config.DATA_DIR / "selected.json").exists()
    if not records and not (args.target == "vectorize" and selected_exists):
        print(
            "data/clean/*.jsonl がありません。先に uv run python prepare.py を実行してください",
            file=sys.stderr,
        )
        return 1

    if args.target == "vectorize":
        try:
            return build_vectorize(records, allow_shrink=args.allow_shrink)
        except (VectorizeCapacityError, VectorizeSelectionError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if not check_index_meta(reset=args.reset):
        return 1

    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    if args.reset:
        try:
            client.delete_collection(config.CHROMA_COLLECTION)
        except Exception:  # noqa: BLE001 - collection may simply not exist yet
            pass

    collection = client.get_or_create_collection(
        name=config.CHROMA_COLLECTION,
        metadata={"hnsw:space": config.CHROMA_SPACE},
    )

    print(f"{len(records)} チャンクを埋め込みます（{config.EMBED_MODEL}）")
    for start in range(0, len(records), config.EMBED_BATCH_SIZE):
        batch = records[start : start + config.EMBED_BATCH_SIZE]
        vectors = embedder.embed([embed_text(record) for record in batch])
        collection.upsert(
            ids=[record["id"] for record in batch],
            embeddings=vectors,
            documents=[record["text"] for record in batch],
            metadatas=[
                {
                    "title": record["title"],
                    "url": record["url"],
                    "last_edited": record["last_edited"],
                    "source": record["source"],
                    "has_image": record["has_image"],
                    "image_links": record["image_links"],
                    "heading_path": record.get("heading_path", ""),
                    "tags": str(record.get("tags", ""))[: config.TAG_METADATA_MAX_CHARS],
                    "embed_model": config.EMBED_MODEL,
                }
                for record in batch
            ],
        )
        print(f"  {min(start + len(batch), len(records))}/{len(records)}")

    _write_index_meta()
    print(f"完了: コレクション '{config.CHROMA_COLLECTION}' に {collection.count()} 件")
    print('次は: uv run python retriever.py "テスト質問"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
