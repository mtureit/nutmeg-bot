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
import vectorize_client


def _load_records() -> list[dict]:
    records: list[dict] = []
    if not config.CLEAN_DIR.exists():
        return records
    for path in sorted(config.CLEAN_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


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


def build_vectorize(records: list[dict]) -> int:
    """Embed every record and upsert into Cloudflare Vectorize, then drop stale ids.

    Every run re-sends all vectors, so an embedding model change is applied in
    full without a separate reset step.
    """
    vectorize_client.require_credentials()
    previous = vectorize_client.load_manifest()
    current = {record["id"] for record in records}

    print(
        f"{len(records)} チャンクを埋め込み、Vectorize '{config.VECTORIZE_INDEX}' に送ります"
        f"（{config.EMBED_MODEL}）"
    )
    with httpx.Client(timeout=60) as client:
        for start in range(0, len(records), config.EMBED_BATCH_SIZE):
            batch = records[start : start + config.EMBED_BATCH_SIZE]
            vectors = embedder.embed([record["text"] for record in batch])
            vectorize_client.upsert(batch, vectors, client=client)
            print(f"  {min(start + len(batch), len(records))}/{len(records)}")

        stale = vectorize_client.stale_ids(previous, current)
        if stale:
            vectorize_client.delete_ids(stale, client=client)
            print(f"  消えたチャンクを {len(stale)} 件削除しました")

    vectorize_client.save_manifest(current)
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
    args = parser.parse_args()

    config.ensure_dirs()

    records = _load_records()
    if not records:
        print(
            "data/clean/*.jsonl がありません。先に uv run python prepare.py を実行してください",
            file=sys.stderr,
        )
        return 1

    if args.target == "vectorize":
        return build_vectorize(records)

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
        vectors = embedder.embed([record["text"] for record in batch])
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
