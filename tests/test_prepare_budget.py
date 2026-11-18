"""Budgeted preparation flow using fake pages and records."""

from __future__ import annotations

import json

import prepare


def simple_page(page_id: str, title: str = "一般") -> dict:
    return {
        "id": page_id,
        "title": title,
        "database": "",
        "last_edited": "2026-10-01T00:00:00Z",
        "url": "",
        "blocks": [],
    }


def metadata_page(page_id: str, title: str = "一般") -> dict:
    page = simple_page(page_id, title)
    page.pop("blocks")
    return page


def isolate_prepare_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "BLOCK_CACHE_DIR", tmp_path / "blocks")
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)


def test_prepare_processes_until_full_and_keeps_filling_after_oversized_page(
    tmp_path, monkeypatch, capsys
):
    pages = [
        {
            "id": "manual",
            "title": "デプロイ手順",
            "database": "",
            "last_edited": "2026-10-01T00:00:00Z",
            "url": "",
            "blocks": [],
        },
        {
            "id": "plan",
            "title": "企画",
            "database": "",
            "last_edited": "2026-10-01T00:00:00Z",
            "url": "",
            "blocks": [],
        },
        {
            "id": "general",
            "title": "一般",
            "database": "",
            "last_edited": "2026-10-01T00:00:00Z",
            "url": "",
            "blocks": [],
        },
        {
            "id": "minutes",
            "title": "MTG",
            "database": "",
            "last_edited": "2026-10-01T00:00:00Z",
            "url": "",
            "blocks": [],
        },
    ]
    sizes = {"manual": 4, "plan": 1, "general": 2, "minutes": 1}
    processed: list[str] = []

    def fake_process(page, *, skip_images):
        processed.append(page["id"])
        return [
            {"id": f"{page['id']}-{index}", "last_edited": page["last_edited"]}
            for index in range(sizes[page["id"]])
        ]

    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids, limit=None: pages)
    monkeypatch.setattr(prepare, "process_page", fake_process)
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "3"])

    assert prepare.main() == 0
    assert processed == ["manual", "plan", "general"]

    selected = json.loads((tmp_path / "data" / "selected.json").read_text(encoding="utf-8"))
    assert selected["total_chunks"] == 3
    assert selected["page_ids"] == ["plan", "general"]
    assert selected["failed_page_ids"] == []
    output = capsys.readouterr().out
    assert "Tier 1" in output
    assert "Tier 4: 0 ページ / 0 チャンク （選択外: 1 ページ）" in output


def test_prepare_skips_selected_json_when_limit_is_used(tmp_path, monkeypatch, capsys):
    pages = [simple_page("page-a", "手順")]

    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids, limit=None: pages)
    monkeypatch.setattr(prepare, "process_page", lambda page, skip_images: [{"id": "page-a-0"}])
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--limit", "1"])

    assert prepare.main() == 0
    assert not (tmp_path / "data" / "selected.json").exists()
    assert "--limit 指定のため更新をスキップ" in capsys.readouterr().out


def test_prepare_skips_selected_json_in_dry_run(tmp_path, monkeypatch, capsys):
    pages = [simple_page("page-a", "手順")]

    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids, limit=None: pages)
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--dry-run"])

    assert prepare.main() == 0
    assert not (tmp_path / "data" / "selected.json").exists()
    assert "--dry-run のため更新しません" in capsys.readouterr().out


def test_prepare_refuses_empty_discovery_without_writing_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids, limit=None: [])
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py"])

    assert prepare.main() == 1
    assert not (tmp_path / "data" / "selected.json").exists()
    assert "0 件" in capsys.readouterr().err


def test_prepare_refuses_failed_root_without_writing_selection(tmp_path, monkeypatch, capsys):
    page = simple_page("page-a", "手順")

    def failed_discovery(root_ids, limit=None):
        failed_discovery.last_failed_roots = ["missing-root"]
        return [page]

    monkeypatch.setattr(prepare, "discover_pages", failed_discovery)
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py"])

    assert prepare.main() == 1
    assert not (tmp_path / "data" / "selected.json").exists()
    assert "ルートページの取得に失敗" in capsys.readouterr().err


def test_prepare_refuses_selection_when_more_than_five_percent_failed(
    tmp_path, monkeypatch, capsys
):
    page = simple_page("page-a", "手順")

    def failed_discovery(root_ids, limit=None):
        failed_discovery.last_failed_pages = ["failed-page"]
        failed_discovery.last_failed_page_titles = ["取得失敗ページ"]
        failed_discovery.last_discovered_page_count = 2
        failed_discovery.last_failed_roots = []
        return [page]

    monkeypatch.setattr(prepare, "discover_pages", failed_discovery)
    monkeypatch.setattr(prepare.config, "NOTION_ROOT_PAGE_IDS", [])
    monkeypatch.setattr(prepare.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(prepare.config, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(prepare.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare.py"])

    assert prepare.main() == 1
    assert not (tmp_path / "data" / "selected.json").exists()
    assert "取得失敗: 1 ページ" in capsys.readouterr().out


def test_phase_b_fetches_in_tier_order_and_stops_at_budget(tmp_path, monkeypatch):
    pages = [
        metadata_page("general", "一般"),
        metadata_page("manual", "デプロイ手順"),
        metadata_page("plan", "企画"),
    ]
    sizes = {"manual": 4, "plan": 1, "general": 2}
    fetched: list[str] = []

    def fake_discovery(root_ids):
        return pages

    def fake_fetch(page_id):
        fetched.append(page_id)
        return []

    def fake_process(page, *, skip_images):
        return [{"id": f"{page['id']}-{index}"} for index in range(sizes[page["id"]])]

    isolate_prepare_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare, "discover_pages", fake_discovery)
    monkeypatch.setattr(prepare, "fetch_block_children", fake_fetch)
    monkeypatch.setattr(prepare, "process_page", fake_process)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "3"])

    assert prepare.main() == 0
    assert fetched == ["manual", "plan", "general"]


def test_phase_b_stops_after_configured_consecutive_non_fitting_pages(
    tmp_path, monkeypatch
):
    pages = [metadata_page(f"page-{index}", "手順") for index in range(4)]
    fetched: list[str] = []

    isolate_prepare_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: pages)
    monkeypatch.setattr(prepare.config, "MAX_CONSECUTIVE_NON_FITTING_PAGES", 3)
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )
    monkeypatch.setattr(
        prepare,
        "process_page",
        lambda page, *, skip_images: [{"id": f"{page['id']}-0"}, {"id": f"{page['id']}-1"}],
    )
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "1"])

    assert prepare.main() == 0
    assert fetched == ["page-0", "page-1", "page-2"]


def test_phase_b_block_cache_hit_avoids_fetch(tmp_path, monkeypatch):
    page = metadata_page("cached", "手順")
    fetched: list[str] = []

    isolate_prepare_paths(tmp_path, monkeypatch)
    prepare._write_block_cache(page["id"], page["last_edited"], [{"id": "cached-block"}])
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: [page])
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )
    monkeypatch.setattr(
        prepare,
        "process_page",
        lambda page, *, skip_images: [{"id": f"{page['id']}-0"}],
    )
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "1"])

    assert prepare.main() == 0
    assert fetched == []


def test_chunker_version_mismatch_rechunks_from_block_cache(tmp_path, monkeypatch):
    page = metadata_page("cached", "手順")
    fetched: list[str] = []
    processed_blocks: list[list[dict]] = []

    isolate_prepare_paths(tmp_path, monkeypatch)
    prepare._write_block_cache(page["id"], page["last_edited"], [{"id": "cached-block"}])
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    (clean_dir / "cached.jsonl").write_text(
        json.dumps(
            {
                "id": "cached-0",
                "last_edited": page["last_edited"],
                "chunker_version": prepare.config.CHUNKER_VERSION - 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: [page])
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )

    def fake_process(content_page, *, skip_images):
        processed_blocks.append(content_page["blocks"])
        return [{"id": "cached-0", "last_edited": page["last_edited"]}]

    monkeypatch.setattr(prepare, "process_page", fake_process)
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "1"])

    assert prepare.main() == 0
    assert fetched == []
    assert processed_blocks == [[{"id": "cached-block"}]]


def test_missing_chunker_version_is_stale(tmp_path, monkeypatch):
    page = metadata_page("legacy", "手順")
    isolate_prepare_paths(tmp_path, monkeypatch)
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    (clean_dir / "legacy.jsonl").write_text(
        json.dumps({"id": "legacy-0", "last_edited": page["last_edited"]}) + "\n",
        encoding="utf-8",
    )

    assert prepare._cached_records(page, force=False) is None


def test_failed_page_is_recorded_and_does_not_stop_phase_b(tmp_path, monkeypatch):
    pages = [metadata_page("bad", "手順")]
    pages.extend(metadata_page(f"page-{index}", "手順") for index in range(19))
    fetched: list[str] = []

    def fake_fetch(page_id):
        fetched.append(page_id)
        if page_id == "bad":
            raise RuntimeError("transient test failure")
        return []

    isolate_prepare_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: pages)
    monkeypatch.setattr(prepare, "fetch_block_children", fake_fetch)
    monkeypatch.setattr(
        prepare,
        "process_page",
        lambda page, *, skip_images: [{"id": f"{page['id']}-0"}],
    )
    monkeypatch.setattr("sys.argv", ["prepare.py", "--budget", "0"])

    assert prepare.main() == 0
    selected = json.loads((tmp_path / "data" / "selected.json").read_text(encoding="utf-8"))
    assert selected["failed_page_ids"] == ["bad"]
    assert len(fetched) == 20


def test_limit_processes_only_first_ordered_candidates(tmp_path, monkeypatch):
    pages = [
        metadata_page("general", "一般"),
        metadata_page("manual", "手順"),
        metadata_page("plan", "企画"),
    ]
    fetched: list[str] = []

    isolate_prepare_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: pages)
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )
    monkeypatch.setattr(
        prepare,
        "process_page",
        lambda page, *, skip_images: [{"id": f"{page['id']}-0"}],
    )
    monkeypatch.setattr("sys.argv", ["prepare.py", "--limit", "2"])

    assert prepare.main() == 0
    assert fetched == ["manual", "plan"]
    assert not (tmp_path / "data" / "selected.json").exists()


def test_dry_run_does_not_fetch_page_blocks(tmp_path, monkeypatch, capsys):
    pages = [metadata_page("manual", "手順")]
    fetched: list[str] = []

    isolate_prepare_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare, "discover_pages", lambda root_ids: pages)
    monkeypatch.setattr(
        prepare,
        "fetch_block_children",
        lambda page_id: fetched.append(page_id) or [],
    )
    monkeypatch.setattr("sys.argv", ["prepare.py", "--dry-run"])

    assert prepare.main() == 0
    assert fetched == []
    assert "Tier 1: 1 ページ" in capsys.readouterr().out
