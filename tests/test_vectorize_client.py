"""Vectorize upload helpers, tested against a fake HTTP transport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

import config
import vectorize_client


def record(i: int, text: str = "本文") -> dict:
    return {
        "id": f"page-{i}",
        "text": text,
        "title": "タイトル",
        "url": "https://www.notion.so/page",
        "last_edited": "2026-10-01T00:00:00.000Z",
        "source": "notion",
        "has_image": False,
        "image_links": "",
        "heading_path": "タイトル > 見出し",
    }


def test_ndjson_has_one_vector_per_line_with_text_in_metadata():
    body = vectorize_client.build_ndjson([record(0), record(1)], [[0.1, 0.2], [0.3, 0.4]])
    lines = [json.loads(line) for line in body.strip().splitlines()]
    assert [line["id"] for line in lines] == ["page-0", "page-1"]
    assert lines[0]["values"] == [0.1, 0.2]
    assert lines[0]["metadata"]["text"] == "本文"
    assert lines[0]["metadata"]["embed_model"] == config.EMBED_MODEL


def test_long_text_is_cut_on_a_utf8_boundary():
    long_text = "あ" * 5000  # 15000 bytes in UTF-8
    meta = vectorize_client.build_metadata(record(0, long_text))
    encoded = meta["text"].encode("utf-8")
    assert len(encoded) <= config.VECTORIZE_TEXT_MAX_BYTES
    assert meta["text"] == "あ" * (len(encoded) // 3)  # no broken trailing character


def test_vector_count_mismatch_is_an_error():
    with pytest.raises(ValueError):
        vectorize_client.build_ndjson([record(0)], [[0.1], [0.2]])


def test_stale_ids_are_previous_minus_current():
    assert vectorize_client.stale_ids({"a", "b", "c"}, {"b"}) == ["a", "c"]


def test_manifest_round_trip_and_other_index_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VECTORIZE_MANIFEST_PATH", tmp_path / "ids.json")
    monkeypatch.setattr(config, "VECTORIZE_INDEX", "idx-a")
    vectorize_client.save_manifest({"x", "y"})
    assert vectorize_client.load_manifest() == {"x", "y"}

    monkeypatch.setattr(config, "VECTORIZE_INDEX", "idx-b")
    assert vectorize_client.load_manifest() == set()


def test_legacy_manifest_without_index_does_not_produce_stale_ids(tmp_path, monkeypatch):
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"ids": ["old-index-id"]}), encoding="utf-8")
    monkeypatch.setattr(config, "VECTORIZE_MANIFEST_PATH", path)
    monkeypatch.setattr(config, "VECTORIZE_INDEX", "nutmeg-bot-512")

    previous = vectorize_client.load_manifest()
    assert previous == set()
    assert vectorize_client.stale_ids(previous, {"new-index-id"}) == []


def test_upsert_and_delete_hit_the_right_endpoints(monkeypatch):
    monkeypatch.setattr(config, "CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(config, "CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr(config, "VECTORIZE_INDEX", "idx")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "result": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vectorize_client.upsert([record(0)], [[0.5]], client=client)
        vectorize_client.delete_ids(["old-1"], client=client)

    assert seen[0].url.path == "/client/v4/accounts/acct/vectorize/v2/indexes/idx/upsert"
    assert seen[0].headers["content-type"] == "application/x-ndjson"
    assert seen[0].headers["authorization"] == "Bearer token"
    assert seen[1].url.path.endswith("/indexes/idx/delete_by_ids")
    assert json.loads(seen[1].content) == {"ids": ["old-1"]}


def test_api_error_is_raised_not_swallowed(monkeypatch):
    monkeypatch.setattr(config, "CLOUDFLARE_ACCOUNT_ID", "acct")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "no auth"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="upsert"):
            vectorize_client.upsert([record(0)], [[0.5]], client=client)


def test_embed_text_prefixes_title_and_heading_path():
    import index_build

    record = {"title": "クイズアプリデプロイに向けて", "heading_path": "とりま練習環境で", "text": "- CT の作成"}
    assert index_build.embed_text(record) == "クイズアプリデプロイに向けて > とりま練習環境で\n\n- CT の作成"
    assert index_build.embed_text({"title": "", "heading_path": "", "text": "x"}) == "x"
