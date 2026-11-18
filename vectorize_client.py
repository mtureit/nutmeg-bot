"""Cloudflare Vectorize isolation layer (REST API).

The Worker answers questions from a Vectorize index, but ingest stays in Python:
crawling Notion takes hundreds of requests and several minutes, which does not
fit a Worker invocation. So index_build.py writes here instead of to Chroma.

The chunk text travels in the vector's metadata, because the Worker has no
other store to look it up from. Metadata is capped at 10 KiB per vector.

Vectorize has no "replace the whole index" call, so ids uploaded last time are
kept in a local manifest and the ones that disappeared (a page got shorter or
was removed) are deleted. Without that, the bot would keep citing text that no
longer exists in Notion.
"""

from __future__ import annotations

import json

import httpx

import config

_API = "https://api.cloudflare.com/client/v4/accounts/{account}/vectorize/v2/indexes/{index}/{op}"
_DELETE_BATCH = 100  # API rejects more than 100 ids per delete_by_ids call


def _url(op: str) -> str:
    return _API.format(
        account=config.CLOUDFLARE_ACCOUNT_ID, index=config.VECTORIZE_INDEX, op=op
    )


def _headers(content_type: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}",
        "Content-Type": content_type,
    }


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def build_metadata(record: dict) -> dict:
    """Metadata the Worker needs to build the prompt and the source links."""
    return {
        "text": _truncate_utf8(record["text"], config.VECTORIZE_TEXT_MAX_BYTES),
        "title": record["title"],
        "url": record["url"],
        "last_edited": record["last_edited"],
        "has_image": bool(record["has_image"]),
        "image_links": record["image_links"],
        "heading_path": record.get("heading_path", ""),
        "embed_model": config.EMBED_MODEL,
    }


def build_ndjson(records: list[dict], vectors: list[list[float]]) -> str:
    lines = [
        json.dumps(
            {"id": record["id"], "values": vector, "metadata": build_metadata(record)},
            ensure_ascii=False,
        )
        for record, vector in zip(records, vectors, strict=True)
    ]
    return "\n".join(lines) + "\n"


def _check(response: httpx.Response, op: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("success", False):
        raise RuntimeError(
            f"Vectorize {op} に失敗しました ({response.status_code}): "
            f"{json.dumps(body.get('errors', response.text[:300]), ensure_ascii=False)}"
        )
    return body


def upsert(records: list[dict], vectors: list[list[float]], *, client: httpx.Client) -> None:
    response = client.post(
        _url("upsert"),
        headers=_headers("application/x-ndjson"),
        content=build_ndjson(records, vectors).encode("utf-8"),
    )
    _check(response, "upsert")


def delete_ids(ids: list[str], *, client: httpx.Client) -> None:
    for start in range(0, len(ids), _DELETE_BATCH):
        response = client.post(
            _url("delete_by_ids"),
            headers=_headers("application/json"),
            json={"ids": ids[start : start + _DELETE_BATCH]},
        )
        _check(response, "delete_by_ids")


def load_manifest() -> set[str]:
    path = config.VECTORIZE_MANIFEST_PATH
    if not path.exists():
        return set()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    # A manifest written for another index, including the old untagged format,
    # says nothing about this one and must never produce deletion candidates.
    if not isinstance(manifest, dict) or manifest.get("index") != config.VECTORIZE_INDEX:
        return set()
    ids = manifest.get("ids", [])
    if not isinstance(ids, list):
        return set()
    return {item for item in ids if isinstance(item, str)}


def save_manifest(ids: set[str]) -> None:
    path = config.VECTORIZE_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"index": config.VECTORIZE_INDEX, "embed_model": config.EMBED_MODEL, "ids": sorted(ids)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def stale_ids(previous: set[str], current: set[str]) -> list[str]:
    return sorted(previous - current)


def require_credentials() -> None:
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        config.require(name)
