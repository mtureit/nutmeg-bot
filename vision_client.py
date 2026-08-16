"""Image download, captioning and caching (PLAN.md 10.1-10.4).

Two things drive this design:

1. Notion-hosted image URLs are presigned and expire in about an hour, so bytes
   are fetched immediately and sent to the model as base64 rather than by URL.
2. The weekly cron re-runs prepare.py. Without a cache every run would re-bill
   every image, so captions are keyed by the sha256 of the image bytes. The URL
   changes on every fetch and must not be used as the key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

import httpx
from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

CAPTION_PROMPT = """これは「{page_title}」というページの「{nearest_heading}」という節にある画像です。
Notion 上のキャプション: {notion_caption}

この画像を、後で全文検索の対象にするための説明文として日本語で記述してください。

- 1行目に画像の種類を書く（図表 / スクリーンショット / 写真 / 手書きメモ / ポスター など）
- 画像に含まれる文字・数値・日付・固有名詞は、読み取れる限りそのまま書き出す
- 表や図の場合は項目名と対応関係がわかるように書く
- 読み取れないものを推測で補わない。不明な箇所は「判読不能」と書く
- 個人の氏名・電話番号・メールアドレス・住所・口座情報が写っている場合は、
  それらを書き出さず「個人情報を含む画像」とだけ記述する
- 全体で400文字以内"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        config.require("OPENAI_API_KEY")
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            max_retries=config.API_MAX_RETRIES,
        )
    return _client


def _extension_for(url: str, content_type: str | None) -> str:
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    if guessed == ".jpe":  # mimetypes returns this for image/jpeg on some systems
        guessed = ".jpg"
    if guessed:
        return guessed
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix or ".bin"


def _cache_path(digest: str) -> Path:
    return config.CAPTIONS_DIR / f"{digest}.json"


def _read_cache(digest: str) -> str | None:
    path = _cache_path(digest)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Regenerate when the model or detail level changed.
    if cached.get("model") != config.VISION_MODEL or cached.get("detail") != config.VISION_DETAIL:
        return None
    return cached.get("caption")


def _write_cache(digest: str, caption: str) -> None:
    config.CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(digest).write_text(
        json.dumps(
            {
                "caption": caption,
                "model": config.VISION_MODEL,
                "detail": config.VISION_DETAIL,
                "created_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _download(url: str) -> tuple[bytes, str | None] | None:
    try:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("image download failed: %s", exc)
        return None
    return response.content, response.headers.get("content-type")


def describe_image(
    block_id: str,
    url: str,
    *,
    page_title: str = "",
    nearest_heading: str = "",
    notion_caption: str = "",
) -> str | None:
    """Return a Japanese description of the image, or None to skip it.

    Failures return None rather than raising: one unreadable image must not
    abort a whole ingest run.
    """
    downloaded = _download(url)
    if downloaded is None:
        return None
    data, content_type = downloaded

    # Cheap filters before spending a vision call (PLAN.md 10.3).
    if len(data) < config.IMAGE_MIN_BYTES:
        logger.info("skipping small image (%d bytes) %s", len(data), block_id)
        return None

    ext = _extension_for(url, content_type)
    if ext not in config.VISION_SUPPORTED_EXT:
        logger.info("skipping unsupported image type %s for %s", ext, block_id)
        return None

    # Keep the bytes so a future model swap can re-caption without re-crawling.
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (config.IMAGES_DIR / f"{block_id}{ext}").write_bytes(data)

    digest = hashlib.sha256(data).hexdigest()
    cached = _read_cache(digest)
    if cached is not None:
        logger.info("caption cache hit for %s", block_id)
        return cached

    prompt = CAPTION_PROMPT.format(
        page_title=page_title or "（不明）",
        nearest_heading=nearest_heading or "（見出しなし）",
        notion_caption=notion_caption or "（なし）",
    )
    mime = content_type.split(";")[0].strip() if content_type else "image/png"
    data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    try:
        response = _get_client().chat.completions.create(
            model=config.VISION_MODEL,
            temperature=config.VISION_TEMPERATURE,
            max_tokens=config.VISION_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": config.VISION_DETAIL},
                        },
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - one bad image must not stop ingest
        logger.warning("captioning failed for %s: %s", block_id, exc)
        return None

    caption = (response.choices[0].message.content or "").strip()
    if not caption:
        return None

    _write_cache(digest, caption)
    return caption
