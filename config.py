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
# Personal profile and casual-chat records are excluded for privacy. 1on1 pages
# remain eligible; their tier is controlled by TAG_TIER4 when tagged.
EXCLUDE_TITLE_KEYWORDS = ["よもやま", "雑談", "アイスブレイク", "メンバーデータベース"]
TAG_TIER1 = [
    "引き継ぎ",
    "SeeFT引き継ぎ",
    "環境構築",
    "デプロイ",
    "勉強会",
    "ドキュメント",
    "PJ_Documents",
]
TAG_TIER4 = [
    "定例MTG",
    "MT",
    "作業ログ",
    "議事録",
    "1on1",
    "よもやま",
    "進捗報告",
    "最強二枚看板会議",
    "PMMT",
    "ヒアリング",
]
TAG_TIER2 = [
    "インフラ",
    "infra",
    "Kubernetes",
    "Docker",
    "Github",
    "DB",
    "api",
    "Go",
    "Next.js",
    "TypeScript",
    "React",
    "Ruby",
    "BE",
    "フロントエンド",
    "NUTMEGクラウド",
    "ハッカソン",
]
TIER1_DATABASE_TITLES = [
    "用語集",
    "引継ぎまとめ",
    "backend-knowledge",
    "デプロイ",
    "k8s, CI/CD",
    "Linux - コマンド",
]
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
CHUNKER_VERSION = 3
CHUNK_TARGET_CHARS = 1200
CHUNK_MIN_CHARS = 300
CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP = 150
TAG_METADATA_MAX_CHARS = 200

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
    "284783e8-ea0d-4059-b30a-c4493ac6efc2",  # Profile (old) database
    "27941f19-2063-8039-acca-f5c5fbea70e1",  # メンバーデータベース database
    "ef1f844d-6740-4d48-8025-8358d472a8a5",  # Profile (2026) database
    "2dd82cae-ae96-4ab1-ba01-56cc83ffef5b",  # Profile (2026) data source
    "f64e82c8-3f8f-40c3-a43a-a27329720153",  # Profile (old) data source
    "27941f19-2063-804f-b861-000b2855ae14",  # メンバーデータベース data source
    "06e8c716-a0fb-4f79-b29d-7a4c22d7a628",  # 個人メモ database
    "0451f7f2-b562-44d7-be81-5bff9da547aa",  # 個人メモ data source
    "35141f19-2063-80be-856c-c1fcc631c3a3",  # メンター振り返り database
    "35141f19-2063-8066-9762-000b1f1c95bf",  # メンター振り返り data source
    "35141f19-2063-8077-8769-e805f7d76005",  # メンター振り返り一覧 database
    "35141f19-2063-80b0-90cc-000bee4b4d51",  # メンター振り返り一覧 data source
    "25aa47a6-0726-473e-927d-8b78d69e85de",  # みんなの予定（空いている時間） database
    "396decef-74bd-4fc9-93a1-0b256ad3cae8",  # みんなの予定（空いている時間） data source
]
# Tag libraries are queried for Tag/Tags name resolution but are not content.
SKIP_CONTENT_IDS: list[str] = [
    "2f29a434-8408-4415-91a5-df96a13ad0e2",  # Tag Library (current) database
    "ac4f4aba-5d42-4bb0-9757-1bd146a880c4",  # Tag Library (current) data source
    "98fb4bcf-ef76-4b7f-aa4b-4a0284bfcc1f",  # Tag Library (old) database
    "371540af-6c36-41c0-8e7a-c97c44169c32",  # Tag Library (old) data source
    "dca1f901-4c1f-471a-a6a0-b7737e9e8678",  # デザインスペース_tags data source
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
