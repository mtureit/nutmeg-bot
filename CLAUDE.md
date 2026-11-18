# nutmeg-bot プロジェクト CLAUDE.md

## 概要
Notion のドメイン知識を RAG で検索し、Slack で質問に答えるナレッジ Bot。
元資料 `NUTMEG_Slackbot_改訂第3版.pdf`（Gemini 前提）を、**回答生成・埋め込みとも OpenAI / 環境 uv** に
読み替えて実装する。Notion 内の画像は VLM でキャプション化して索引に載せる。
詳細な設計・タスク・手順は **PLAN.md** に集約（実装の設計書兼チェックリスト）。

## 重要な設計決定
- **回答生成**: OpenAI `gpt-4.1-mini`（temperature=0.2）。影響は `llm_client.py` のみ
  - `gpt-5-mini` 等の推論系は `temperature` が使えないため採用しない（事実重視の設計と噛み合わない）
- **埋め込み**: OpenAI `text-embedding-3-small`（512次元、`dimensions=512`）。ローカル e5 から API へ変更
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
- **Notion データベース対応**（2026-10-16 追加）: ページ内の `child_database` を**どの深さでも**探し
  （列・コールアウトの中も）、各行をページとして取り込む。行のプロパティは `- 名前: 値` で本文の前に置く。
  API 2025-09-03 のため行は `databases.retrieve` → `data_sources.query` で取る（`databases.query` は旧方式）。
  `Tag` / `Tags` relation は参照先データソースを一度だけ全件取得してタグ名に解決する。has_more の relation は
  `pages.properties.retrieve` で追加取得し、タグ以外の relation はIDを本文に出さない。
  ルート ID に DB の ID を直接書いてもよい。Tag/Tags 以外の relation / rollup はIDを出力しない。
  リンクドビュー（`unsupported` ブロック）は API が中身を返さないため読めない。元の DB を接続すること
  - ルート ID モードは探索時にブロックを全階層取得し、レコードに保持して `process_page` で再利用する（二重取得しない）。
    token-only モードは Phase A で検索結果と DB 行のメタデータだけを集め、Phase B で優先順位・予算順に必要なページの
    ブロックだけを取得する。取得は指数バックオフで再試行し、成功した再帰ブロックは `data/cache/blocks/` に
    `last_edited_time` 付きで保存する。取得失敗ページは `selected.json` に記録して前回の索引を残し、必要なら
    `--no-block-cache` でキャッシュを無効化する
- **Notion 取得**: `NOTION_ROOT_PAGE_IDS` が空なら `/v1/search` で接続済みの全部を発見し、
  `NOTION_EXCLUDE_PAGE_IDS` で部分木を除外する。範囲は `--dry-run` で確認する（Notion で接続を追加すると
  取り込み範囲も広がるリスクがある）
  - Phase A はブロックを取得せずタイトル・DB 名・更新時刻で Tier を決め、Phase B は Tier 順に予算が埋まるまで
    取得する。ブロックは 100 件/リクエスト・`has_children` で再帰し、`--dry-run` は Phase A の集計だけを行う
- **認証情報の伏字化**: `redact.py` を `prepare.py` のチャンク前と `index_build.py` の読み込み時に適用し、
  `[伏字]` に置換する。自由文のパスワードは漏れる可能性があり、`uv run python redact.py --check` で要確認箇所を列挙する
- **Vectorize のページ選定**: 無料枠 5,000,000 保存次元（512 次元なら 9,765 ベクトル）に
  約 3% の余裕を残し、`config.CHUNK_BUDGET` の既定値 9,500 チャンク以内で選ぶ。
  `selection.py` がページタイトルと親データベース名を判定し、手順・規約・設計などを Tier 1、
  企画・要件などを Tier 2、その他を Tier 3、議事録・定例・日付タイトルなどを Tier 4 とする。
  タグTier 1 → Tier 1 DB名 → タグTier 4 → タグTier 2 → 既存ルールの順で判定する。タグによる除外は行わず、
  `1on1` も許可する。Profile、メンバー／メンター個人DB、個人メモ、予定DBはDB／データソースIDを
  `config.NOTION_EXCLUDE_PAGE_IDS` に併記して祖先判定で除外する。
  `prepare.py` の `--budget 0` は無制限。実行時の `data/selected.json` を `index_build.py` が読み、
  選択外 JSONL を除外して stale チャンクを削除する。無料枠超過は埋め込み前に停止する。
- **チャンクと増分更新**: `chunking.py` は見出しセクションを `CHUNK_TARGET_CHARS=1200` まで連結し、
  `CHUNK_MAX_CHARS=1500` を超える長いセクションだけをオーバーラップ分割する。タイトル・プロパティは最初の本文と
  結合する。JSONL の `chunker_version` が現在値と異なるページは、`data/cache/blocks/` を再利用して再チャンクする。
- **Worker の近傍展開**: しきい値を通った Vectorize ヒットの前後チャンクを `getByIds` で取得し、ページごとに重複除去・
  文書順連結する。ページ単位のコンテキストは約 12,000 文字に制限し、出典はページごとに一つにまとめる。
- **セキュリティ**: PLAN.md **§17** に独立記載。要点は3つ
  - **インテグレーショントークンには Notion の人単位アクセス権が反映されない**。接続したページは
    Slack 全員に公開されると考える。第一防衛線は Notion 側「接続を追加」の最小化（§17.1）
  - **Notion 本文は信頼できない入力**として扱う。プロンプトインジェクション対策をシステムプロンプトに
    組み込む（§17.2、§9 に反映済み）
  - 画像キャプション化は**新しい漏洩経路**。画像内の文字が平文化され Slack から検索可能になる（§17.3）
  - 公開前に §17.9 のチェックリストを実施する
- **設計判断の確認済み**: 埋め込み API=OpenAI、生成も同一プロバイダに統一、画像は VLM キャプション化
  の3点をユーザーが承認（2026-07-29）。同日、Notion 取得方式（ルート起点再帰）と §17 の新設も承認

## 現在の状況（最終更新: 2026-10-16）
- **branch 構成**: `python-socket-mode` = Python 版（Socket Mode + Chroma）を凍結。
  `cloudflare-worker` = Worker 版を開発中（回答側を `worker/` の TypeScript に、索引を Vectorize に移した。
  取り込みは Python のまま `index_build.py --target vectorize`）。詳細は **worker/README.md**
  - デプロイ先は **mtureit 個人アカウント**（`4a114bfd…`）。雲居アカウントには載せない。
    Worker <https://nutmeg-bot.mtureit.workers.dev> は 2026-10-16 にデプロイ済みだが secret 未設定（全リクエスト 401）
- **2026-10-16**: 個人 Notion（研究管理ページ、DB 4 つ・計 19 行）で初の実接続。`--no-images` で 20 ページ /
  161 チャンクを索引化し、`retriever.py` で DB のプロパティ（締切・著者等）まで検索できることを確認。
  `uv run pytest` は 35 件成功。**画像込みの取り込み・しきい値校正・Slack 接続は未実施**
  - 気づき: しきい値 0.35 では無関係なチャンク（類似度 0.37 前後）も通る。校正（Phase 6）で上げる見込み

- **Phase 1〜5 の実装完了**。全モジュールが鍵なしで import でき、`uv run pytest` は 26 件成功
- git 初期化済み・remote 設定済み（<https://github.com/mtureit/nutmeg-bot>、**public**）。**未 push**
- **未実行**: Notion / OpenAI / Slack への実接続。鍵が未発行のため

### 実装済みファイル
`config.py` / `chunking.py` / `notion_md.py` / `embedder.py` / `vision_client.py` /
`llm_client.py` / `retriever.py` / `prepare.py` / `selection.py` / `index_build.py` / `bot.py` /
`eval_threshold.py` / `tests/`（3ファイル）/ `README.md` / `pyproject.toml` / `.gitignore`

- `PLAN.md` §5 から**逸脱1点**: Notion ブロック→Markdown 変換を `notion_md.py` に分離した。
  ネットワーク非依存にしてユニットテスト可能にするため。PLAN.md §5 も更新済み
- **`.env` のひな形は `sample.env`**（2026-10-16 作成）。`.env.example` は権限設定の deny ルール
  （`Read(**/.env.*)`）に掛かるため、この名前にしている

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
