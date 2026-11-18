"""Selected-page filtering and Vectorize safety checks without network access."""

from __future__ import annotations

import json

import pytest

import config
import index_build


def record(record_id: str) -> dict:
    return {
        "id": record_id,
        "text": "本文",
        "title": "タイトル",
        "url": "https://www.notion.so/page",
        "last_edited": "2026-10-01T00:00:00.000Z",
        "source": "notion",
        "has_image": False,
        "image_links": "",
        "heading_path": "",
    }


def write_jsonl(path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )


def test_load_records_uses_selected_page_ids_and_reports_skipped_files(
    tmp_path, monkeypatch, capsys
):
    clean_dir = tmp_path / "clean"
    data_dir = tmp_path / "data"
    clean_dir.mkdir()
    data_dir.mkdir()
    write_jsonl(clean_dir / "pagea.jsonl", [record("page-a-0")])
    write_jsonl(clean_dir / "pageb.jsonl", [record("page-b-0")])
    (data_dir / "selected.json").write_text(
        json.dumps({"budget": 10, "total_chunks": 1, "page_ids": ["page-a"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CLEAN_DIR", clean_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)

    records = index_build._load_records()

    assert [item["id"] for item in records] == ["page-a-0"]
    assert "1 件スキップ" in capsys.readouterr().out


def test_unselected_previous_vectors_are_deleted(monkeypatch):
    records = [record("selected-page-0")]
    deleted: list[str] = []

    monkeypatch.setattr(index_build.vectorize_client, "require_credentials", lambda: None)
    monkeypatch.setattr(
        index_build.vectorize_client, "load_manifest", lambda: {"selected-page-0", "old-page-0"}
    )
    monkeypatch.setattr(index_build.vectorize_client, "upsert", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        index_build.vectorize_client, "delete_ids", lambda ids, **kwargs: deleted.extend(ids)
    )
    monkeypatch.setattr(index_build.vectorize_client, "save_manifest", lambda ids: None)
    monkeypatch.setattr(
        index_build.embedder,
        "embed",
        lambda texts: [[0.0] * config.EMBED_DIM for _ in texts],
    )

    assert index_build.build_vectorize(records, allow_shrink=True) == 0
    assert deleted == ["old-page-0"]


def test_failed_page_vectors_are_kept_during_stale_deletion(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "selected.json").write_text(
        json.dumps(
            {
                "budget": 10,
                "total_chunks": 1,
                "page_ids": ["new-page"],
                "failed_page_ids": ["failed-page"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CLEAN_DIR", tmp_path / "clean")

    records = [record("new-page-0")]
    deleted: list[str] = []
    saved: list[set[str]] = []
    monkeypatch.setattr(index_build.vectorize_client, "require_credentials", lambda: None)
    monkeypatch.setattr(
        index_build.vectorize_client,
        "load_manifest",
        lambda: {"new-page-0", "failed-page-0", "old-page-0"},
    )
    monkeypatch.setattr(
        index_build.vectorize_client, "delete_ids", lambda ids, **kwargs: deleted.extend(ids)
    )
    monkeypatch.setattr(
        index_build.vectorize_client, "save_manifest", lambda ids: saved.append(set(ids))
    )
    monkeypatch.setattr(
        index_build.embedder,
        "embed",
        lambda texts: [[0.0] * config.EMBED_DIM for _ in texts],
    )
    monkeypatch.setattr(index_build.vectorize_client, "upsert", lambda *args, **kwargs: None)

    assert index_build.build_vectorize(records, allow_shrink=True) == 0
    assert deleted == ["old-page-0"]
    assert saved == [{"new-page-0", "failed-page-0"}]


def test_vectorize_capacity_guard_runs_before_embedding(monkeypatch):
    called = False

    def fail_if_called(texts):
        nonlocal called
        called = True
        raise AssertionError("embedding must not start")

    monkeypatch.setattr(index_build.embedder, "embed", fail_if_called)
    records = [{"id": f"page-{index}"} for index in range(config.FREE_TIER_MAX_VECTORS + 1)]

    with pytest.raises(index_build.VectorizeCapacityError, match="無料枠"):
        index_build.build_vectorize(records)

    assert called is False


def test_vectorize_deletes_stale_ids_before_embedding(monkeypatch):
    events: list[str] = []
    records = [record("new-page-0")]

    monkeypatch.setattr(index_build.vectorize_client, "require_credentials", lambda: None)
    monkeypatch.setattr(
        index_build.vectorize_client,
        "load_manifest",
        lambda: {"new-page-0", "old-page-0"},
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "delete_ids",
        lambda ids, **kwargs: events.append(f"delete:{len(ids)}"),
    )
    monkeypatch.setattr(
        index_build.embedder,
        "embed",
        lambda texts: events.append("embed") or [[0.0] * config.EMBED_DIM for _ in texts],
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "upsert",
        lambda records, vectors, **kwargs: events.append("upsert"),
    )
    monkeypatch.setattr(index_build.vectorize_client, "save_manifest", lambda ids: None)

    assert index_build.build_vectorize(records, allow_shrink=True) == 0
    assert events == ["delete:1", "embed", "upsert"]


def test_vectorize_aborts_empty_selection_before_credentials_or_delete(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        index_build.vectorize_client,
        "load_manifest",
        lambda: {"old-page-0"},
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "require_credentials",
        lambda: called.append("credentials"),
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "delete_ids",
        lambda ids, **kwargs: called.append("delete"),
    )

    with pytest.raises(index_build.VectorizeSelectionError, match="0 件"):
        index_build.build_vectorize([])

    assert called == []


def test_vectorize_aborts_when_stale_deletions_exceed_thirty_percent(monkeypatch):
    previous = {f"old-{index}" for index in range(10)}
    records = [record(f"current-{index}") for index in range(6)]
    called: list[str] = []

    monkeypatch.setattr(index_build.vectorize_client, "load_manifest", lambda: previous)
    monkeypatch.setattr(
        index_build.vectorize_client,
        "require_credentials",
        lambda: called.append("credentials"),
    )
    monkeypatch.setattr(
        index_build.vectorize_client,
        "delete_ids",
        lambda ids, **kwargs: called.append("delete"),
    )
    monkeypatch.setattr(
        index_build.embedder,
        "embed",
        lambda texts: called.append("embed") or [],
    )

    with pytest.raises(index_build.VectorizeSelectionError, match="以前 10 チャンク中、古い 10 チャンク"):
        index_build.build_vectorize(records)

    assert called == []
