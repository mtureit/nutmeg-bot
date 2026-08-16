# nutmeg-bot プロジェクト CLAUDE.md

## 概要
Notion のドメイン知識を RAG で検索し、Slack で質問に答えるナレッジ Bot。
元資料 `NUTMEG_Slackbot_改訂第3版.pdf`（Gemini 前提）を、**回答生成・埋め込みとも OpenAI / 環境 uv** に
読み替えて実装する。Notion 内の画像は VLM でキャプション化して索引に載せる。
詳細な設計・タスク・手順は **PLAN.md** に集約（実装の設計書兼チェックリスト）。

## 重要な設計決定
- **回答生成**: OpenAI `gpt-4.1-mini`（temperature=0.2）。影響は `llm_client.py` のみ
  - `gpt-5-mini` 等の推論系は `temperature` が使えないため採用しない（事実重視の設計と噛み合わない）
- **埋め込み**: OpenAI `text-embedding-3-small`（1536次元）。ローカル e5 から API へ変更
  - `sentence-transformers`（torch 約2〜3GB）が依存から消え、サーバー配備が軽くなった
  - prefix 規約（`query: `/`passage: `）は不要。e5 前提の記述が残っていたら誤り
  - 反面、質問時も外部 API を叩くため OpenAI 障害時は Bot が回答不能になる
- **画像**: VLM（`gpt-4.1-mini`, detail=high）でキャプション化し、画像位置にインラインで
  Markdown へ差し込んでテキストとして索引。マルチモーダル埋め込みは使わない
  - Notion の画像 URL は**署名付きで約1時間で失効**する。VLM へは base64 でバイト列を送り、
    回答に載せるリンクは恒久的な Notion ブロックディープリンクを使う
  - キャプションキャッシュのキーは**画像バイト列の sha256**（URL は取得ごとに変わるので使えない）。
    週次 cron があるためキャッシュ無しでは毎週全画像に課金が発生する
- **しきい値**: `SIM_THRESHOLD` は e5 前提の 0.75 から **暫定 0.35** へ変更。Phase 6 で
  `eval_threshold.py` による実測校正が**必須**。0.75 のままだと全質問が「見つかりません」になる
  - Chroma は `space=cosine` で**距離**を返す。`similarity = 1 - distance` に変換して比較する
- **パッケージ管理**: uv（pip 禁止）
- **ベクトル DB**: Chroma（`space=cosine`、しきい値を解釈しやすくするため）
- **アーキテクチャ**: 外部依存を薄い層に隔離。2 段階パイプライン（prepare → JSONL → index_build）
  - `embed(texts) -> vectors` の署名は変えない（上位は埋め込みがローカルか API かを知らない）
  - `prepare.py` は `openai` を直接 import しない（画像処理は `vision_client.py` 経由）
- **Notion 取得**: `.env` の `NOTION_ROOT_PAGE_IDS` 起点で再帰的にページを辿る（PLAN.md §10.0）。
  `/v1/search` は使わない — 「共有された全部」という暗黙スコープになり取り込み範囲が勝手に広がるため
  - ブロックは 100 件/リクエスト・`has_children` で再帰。300ページ規模で 5〜8分かかる
- **セキュリティ**: PLAN.md **§17** に独立記載。要点は3つ
  - **インテグレーショントークンには Notion の人単位アクセス権が反映されない**。接続したページは
    Slack 全員に公開されると考える。第一防衛線は Notion 側「接続を追加」の最小化（§17.1）
  - **Notion 本文は信頼できない入力**として扱う。プロンプトインジェクション対策をシステムプロンプトに
    組み込む（§17.2、§9 に反映済み）
  - 画像キャプション化は**新しい漏洩経路**。画像内の文字が平文化され Slack から検索可能になる（§17.3）
  - 公開前に §17.9 のチェックリストを実施する
- **設計判断の確認済み**: 埋め込み API=OpenAI、生成も同一プロバイダに統一、画像は VLM キャプション化
  の3点をユーザーが承認（2026-07-29）。同日、Notion 取得方式（ルート起点再帰）と §17 の新設も承認

## 現在の状況（最終更新: 2026-07-29）
- **Phase 1〜5 の実装完了**。全モジュールが鍵なしで import でき、`uv run pytest` は 26 件成功
- git 初期化済み・remote 設定済み（<https://github.com/mtureit/nutmeg-bot>、**public**）。**未 push**
- **未実行**: Notion / OpenAI / Slack への実接続。鍵が未発行のため

### 実装済みファイル
`config.py` / `chunking.py` / `notion_md.py` / `embedder.py` / `vision_client.py` /
`llm_client.py` / `retriever.py` / `prepare.py` / `index_build.py` / `bot.py` /
`eval_threshold.py` / `tests/`（3ファイル）/ `README.md` / `pyproject.toml` / `.gitignore`

- `PLAN.md` §5 から**逸脱1点**: Notion ブロック→Markdown 変換を `notion_md.py` に分離した。
  ネットワーク非依存にしてユニットテスト可能にするため。PLAN.md §5 も更新済み
- **`.env.example` は未作成**。権限設定の deny ルールでブロックされたため、
  テンプレートは README「2. 環境変数」に記載してある

### 次にやること
1. ユーザーが鍵を発行（Notion / OpenAI / Slack。手順は PLAN.md §12）し `.env` を作成
2. `uv run python prepare.py --dry-run` で取り込み範囲を確認（課金ゼロ）
3. `--no-images --limit 5` → `--limit 5` → 全体、と段階的に広げる
4. `index_build.py` → `retriever.py "質問"` で検索を確認
5. `eval/questions.tsv` を作り `eval_threshold.py` で `SIM_THRESHOLD` を確定（**必須**）
6. Phase 7（systemd / cron）と PLAN.md §17.9 のチェックリスト

## 注意
- **git**: `init` / `remote add` / `commit` は Claude が行う。**push はユーザーが実行**
  （2026-07-29 にユーザーが指定）。`git reset --hard` と force push は行わない
- **リポジトリは public**。push 前に `git show --stat` で内容を確認する
- 鍵・トークンは `.env` 管理、Git に含めない
- モデル ID・単価は OpenAI 公式ドキュメントで再確認する（未確認のまま記載している）
