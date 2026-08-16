"""Central configuration for nutmeg-bot.

All tunable constants live here so that model names, chunk sizes, thresholds and
ingest scope can be reviewed in one place (PLAN.md 6, 17).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- paths ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CLEAN_DIR = DATA_DIR / "clean"
IMAGES_DIR = DATA_DIR / "images"
CAPTIONS_DIR = DATA_DIR / "captions"
CHROMA_DIR = BASE_DIR / "chroma_db"
LOGS_DIR = BASE_DIR / "logs"
INDEX_META_PATH = CHROMA_DIR / "index_meta.json"

# --- embeddings (OpenAI) ---
# No "query:"/"passage:" prefix needed. Pricing must be confirmed against the
# official OpenAI pricing page before rollout.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536  # reducible via the dimensions parameter; changing it forces a full re-index
EMBED_BATCH_SIZE = 100  # API limit is 2048 inputs per request / 8191 tokens per input

# --- chunking ---
CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP = 150

# --- retrieval ---
TOP_K = 5
CHROMA_SPACE = "cosine"  # Chroma returns a distance; similarity = 1 - distance
CHROMA_COLLECTION = "nutmeg"
SIM_THRESHOLD = 0.35  # provisional; calibrate with eval_threshold.py (PLAN.md 11)

# --- generation (OpenAI) ---
GEN_MODEL = "gpt-4.1-mini"
GEN_TEMPERATURE = 0.2  # low, to keep answers grounded in the retrieved context
GEN_MAX_TOKENS = 1024

# --- image captioning (OpenAI vision) ---
VISION_MODEL = "gpt-4.1-mini"
VISION_DETAIL = "high"  # "low" downscales to 512px and destroys small text in diagrams
VISION_MAX_TOKENS = 400
VISION_TEMPERATURE = 0.0  # keeps captions stable across re-ingests
IMAGE_MIN_BYTES = 10_000  # below this an image is treated as an icon/decoration
VISION_SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# --- rate limiting ---
NOTION_SLEEP_SEC = 0.35  # Notion allows roughly 3 requests/second on average
API_MAX_RETRIES = 5  # 429 / 5xx are retried with exponential backoff

# --- ingest scope and access control (security boundary; PLAN.md 17) ---
NOTION_EXCLUDE_PAGE_IDS: list[str] = []  # skipped even when under a root page
ALLOWED_SLACK_CHANNELS: list[str] = []  # empty list = every channel allowed
ALLOW_GUEST_USERS = False  # refuse single-channel / multi-channel guests

# --- user-facing strings ---
NOT_FOUND_MESSAGE = "Notion に情報が見つかりませんでした"
ERROR_MESSAGE = "一時的にエラーが発生しました。しばらくしてからもう一度お試しください。"

# --- secrets (from .env; never hard-code) ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")


def _split_ids(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


NOTION_ROOT_PAGE_IDS = _split_ids(os.getenv("NOTION_ROOT_PAGE_IDS", ""))


def normalize_id(page_or_block_id: str) -> str:
    """Strip dashes so IDs from URLs and from the API compare equal."""
    return page_or_block_id.replace("-", "").strip().lower()


def require(name: str) -> str:
    """Fetch a required secret, failing with an actionable message."""
    value = globals().get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} が設定されていません。.env を作成し値を入れてください"
            "（テンプレートは README の「環境変数」節、手順は PLAN.md §12）"
        )
    return value


def ensure_dirs() -> None:
    for directory in (CLEAN_DIR, IMAGES_DIR, CAPTIONS_DIR, CHROMA_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
