"""Chroma nearest-neighbour search with a similarity threshold (PLAN.md 9).

Chroma returns a *distance* when the space is cosine, so it is converted back to
a similarity before comparing against SIM_THRESHOLD. The threshold value is
model-dependent: 0.35 is a starting point for text-embedding-3-small and must be
calibrated with eval_threshold.py against real questions.

Embedding failures propagate as exceptions. Returning "not found" on an API
error would tell users the knowledge base lacks the answer, which is a lie.
"""

from __future__ import annotations

import sys

import chromadb

import config
import embedder
import index_build

_collection = None


def _check_index_meta() -> None:
    meta = index_build._read_index_meta()
    if meta is None:
        return
    if meta.get("embed_model") == config.EMBED_MODEL and meta.get("embed_dim") == config.EMBED_DIM:
        return
    raise RuntimeError(
        "ローカル Chroma 索引の埋め込み設定が現在の config と異なります。"
        f" 索引: {meta.get('embed_model')} / {meta.get('embed_dim')} 次元、"
        f"現在: {config.EMBED_MODEL} / {config.EMBED_DIM} 次元。"
        "uv run python index_build.py --reset を実行して全再索引してください。"
    )


def get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        _collection = client.get_or_create_collection(
            name=config.CHROMA_COLLECTION,
            metadata={"hnsw:space": config.CHROMA_SPACE},
        )
    return _collection


def search_all(question: str, k: int = config.TOP_K) -> list[dict]:
    """Return the top k hits with similarities, without applying the threshold."""
    _check_index_meta()
    vector = embedder.embed_query(question)
    result = get_collection().query(query_embeddings=[vector], n_results=k)

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]

    return [
        {
            "text": document,
            "metadata": metadata or {},
            "similarity": 1.0 - distance,
        }
        for document, metadata, distance in zip(documents, metadatas, distances, strict=False)
    ]


def search(question: str, k: int = config.TOP_K) -> list[dict] | None:
    """Return contexts, or None when nothing clears the similarity threshold."""
    hits = search_all(question, k)
    if not hits or hits[0]["similarity"] < config.SIM_THRESHOLD:
        return None
    return [hit for hit in hits if hit["similarity"] >= config.SIM_THRESHOLD]


def main() -> int:
    """CLI search, so retrieval can be checked before standing up Slack."""
    if len(sys.argv) < 2:
        print('使い方: uv run python retriever.py "質問"', file=sys.stderr)
        return 1

    question = " ".join(sys.argv[1:])
    hits = search_all(question)
    if not hits:
        print("インデックスが空です。prepare.py と index_build.py を先に実行してください")
        return 1

    print(f"しきい値: {config.SIM_THRESHOLD}\n")
    for i, hit in enumerate(hits, start=1):
        mark = "OK " if hit["similarity"] >= config.SIM_THRESHOLD else "下回る"
        meta = hit["metadata"]
        print(f"[{i}] {mark} similarity={hit['similarity']:.3f}  {meta.get('title', '')}")
        print(f"    {meta.get('heading_path', '')}")
        preview = hit["text"].replace("\n", " ")[:120]
        print(f"    {preview}...\n")

    if hits[0]["similarity"] < config.SIM_THRESHOLD:
        print(f"→ 最上位がしきい値未満のため Bot は「{config.NOT_FOUND_MESSAGE}」を返します")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
