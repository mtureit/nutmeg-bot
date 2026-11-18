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
BLOCK_CACHE_DIR = DATA_DIR / "cache" / "blocks"
CHROMA_DIR = BASE_DIR / "chroma_db"
LOGS_DIR = BASE_DIR / "logs"
INDEX_META_PATH = CHROMA_DIR / "index_meta.json"

# --- embeddings (OpenAI) ---
# No "query:"/"passage:" prefix needed. Pricing must be confirmed against the
# official OpenAI pricing page before rollout.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 512  # must match worker/src/config.ts; changing it forces a full re-index
EMBED_BATCH_SIZE = 100  # API limit is 2048 inputs per request / 8191 tokens per input
FREE_TIER_STORED_DIMS = 5_000_000
FREE_TIER_MAX_VECTORS = FREE_TIER_STORED_DIMS // EMBED_DIM
# Keep about 3% headroom below FREE_TIER_MAX_VECTORS for the default selection.
CHUNK_BUDGET = int(
    os.getenv("CHUNK_BUDGET", str(FREE_TIER_MAX_VECTORS * 97 // 100))
)

# --- page selection ---
# Personal records (1on1 notes, member profiles) are excluded for privacy.
EXCLUDE_TITLE_KEYWORDS = ["よもやま", "雑談", "アイスブレイク", "1on1", "メンバーデータベース"]
TIER1_KEYWORDS = [
    "手順",
    "マニュアル",
    "引き継ぎ",
    "引継ぎ",
    "規約",
    "ルール",
    "仕様",
    "設計",
    "README",
    "ガイド",
    "how to",
    "運用",
    "環境構築",
    "セットアップ",
    "デプロイ",
]
TIER2_KEYWORDS = ["企画", "プロジェクト", "概要", "要件", "振り返り"]
TIER4_KEYWORDS = ["MT", "mtg", "ミーティング", "定例", "議事録"]

# --- chunking ---
CHUNKER_VERSION = 2
CHUNK_TARGET_CHARS = 1200
CHUNK_MIN_CHARS = 300
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
NOTION_INITIAL_RETRY_DELAY_SEC = 1.0
NOTION_MAX_RETRY_DELAY_SEC = 30.0
MAX_CONSECUTIVE_NON_FITTING_PAGES = 200

# --- ingest scope and access control (security boundary; PLAN.md 17) ---
# Skipped even when under a root page. Databases of personal profiles
# (phone, birthday, email) are excluded for privacy.
NOTION_EXCLUDE_PAGE_IDS: list[str] = [
    "284783e8-ea0d-4059-b30a-c4493ac6efc2",  # Profile
    "27941f19-2063-8039-acca-f5c5fbea70e1",  # メンバーデータベース
]
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


# --- Cloudflare Vectorize (the index the Worker reads; see worker/README.md) ---
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")  # needs Vectorize: Edit
VECTORIZE_INDEX = os.getenv("VECTORIZE_INDEX", "nutmeg-bot-512")
VECTORIZE_MANIFEST_PATH = DATA_DIR / "vectorize_ids.json"  # ids plus the index name uploaded last time
VECTORIZE_TEXT_MAX_BYTES = 8000  # metadata limit is 10 KiB per vector; leave room for the rest


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
    for directory in (
        CLEAN_DIR,
        IMAGES_DIR,
        CAPTIONS_DIR,
        BLOCK_CACHE_DIR,
        CHROMA_DIR,
        LOGS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
