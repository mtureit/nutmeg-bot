# NUTMEG Knowledge Bot

Notion に蓄積された NUTMEG のドメイン知識を RAG（検索拡張生成）で検索し、Slack でメンションすると
出典つきで答える Bot。

設計の詳細は [PLAN.md](PLAN.md)、セキュリティ設計は [PLAN.md §17](PLAN.md) を参照。

## 構成

```
Notion API → prepare.py → data/clean/*.jsonl → index_build.py → chroma_db/
              (画像は vision_client.py でキャプション化)

Slack @mention → bot.py → retriever.py → llm_client.py → 出典付き返信
```

| 種別 | 使うもの |
|---|---|
| 埋め込み | OpenAI `text-embedding-3-small`（512次元） |
| 回答生成 | OpenAI `gpt-4.1-mini`（temperature=0.2） |
| 画像キャプション | OpenAI `gpt-4.1-mini`（vision, detail=high） |
| ベクトル DB | Chroma（`space=cosine`） |
| Slack | Bolt / Socket Mode（公開 URL 不要） |

## セットアップ

### 1. 依存

```bash
uv sync
```

uv が Python 3.12 を自動で用意する（システム Python は変更されない）。

### 2. 環境変数

ひな形 `sample.env` をコピーして `.env` を作り、値を埋める（`cp sample.env .env && chmod 600 .env`）。
内容は次のとおり。**`.env` はコミットしない**（`.gitignore` 済み）。
各キーの取得手順は [PLAN.md §12](PLAN.md)。

```bash
# --- Notion ---
# https://www.notion.so/my-integrations で内部インテグレーションを作成
NOTION_TOKEN=

# 取り込みの起点ページ ID をカンマ区切りで指定（取り込み範囲の allowlist）
# ここに書いた配下のみ取り込む。Notion 側でも各ページに「接続を追加」が必要
NOTION_ROOT_PAGE_IDS=

# --- OpenAI（埋め込み・生成・画像の3用途を1鍵に集約） ---
# https://platform.openai.com で発行し、Usage limits で月額上限を必ず設定する
OPENAI_API_KEY=

# --- Slack ---
# スコープ: chat:write / app_mentions:read / users:read
SLACK_BOT_TOKEN=xoxb-
# Socket Mode の App-Level Token（スコープ: connections:write）
SLACK_APP_TOKEN=xapp-
```

### 3. 取り込みは段階的に広げる

いきなり全件を取り込まないこと。**`--dry-run` → 画像なし → 小規模 → 全体**の順に広げると、
取り込み範囲の誤りと画像内の個人情報を、課金と公開の前に検出できる。

```bash
# ① 課金ゼロ。取り込み対象のページ一覧を確認する
uv run python prepare.py --dry-run

# ② 画像なし・5ページだけで本文パイプラインを確認
uv run python prepare.py --no-images --limit 5
#    → data/clean/*.jsonl を目視（チャンク境界・メタデータ）

# ③ 画像込みで小さく試す
uv run python prepare.py --limit 5
#    → data/captions/ を目視（個人情報が書き出されていないか）

# ④ 全体
uv run python prepare.py
```

### 3.5 Vectorize の無料枠とページ選定

Cloudflare Vectorize の無料枠は保存次元数 5,000,000 です。512 次元のチャンクを
9,765 件まで保存できますが、`prepare.py` は約 3% の余裕を残して既定の
`CHUNK_BUDGET=9500` を使います。`--budget N` または環境変数 `CHUNK_BUDGET` で変更でき、
`--budget 0` は無制限です。

ページは `config.py` のキーワード・タグリストで優先順位を決めます。タグは Notion の
`Tag` / `Tags` relation から名前を解決し、Tier 1 タグ → Tier 1 データベース名 → Tier 4 タグ
→ Tier 2 タグ → 既存のタイトル・日付ルールの順で判定します。タグによる除外は行いません。
よもやま・雑談・アイスブレイクはタイトルキーワードとして常に除外します（`1on1` は許可）。
除外対象の Profile、メンバー／メンターの個人DB、個人メモ、予定DBは `config.py` の
`NOTION_EXCLUDE_PAGE_IDS` にDB IDとデータソースIDを併記しています。除外・キーワードは
`config.py` で編集できます。
Tier 判定は Phase A のメタデータだけで行い、本文ブロックは Phase B で優先順位と予算に応じて遅延取得します。

実行時に `data/selected.json` が選ばれたページ ID とチャンク数を記録します。
`index_build.py --target vectorize` はこの一覧にない JSONL を読み込まず、前回の索引に
だけ残ったチャンクを削除します。また、無料枠を超える件数は埋め込み前に停止します。

Notion 読み出しは、`NOTION_ROOT_PAGE_IDS` が空なら Phase A で `/v1/search` と接続済みデータベースの行から
メタデータだけを集め、Tier と更新時刻の順に Phase B で必要なページのブロックを遅延取得します。予算が埋まると
未選択ページを取得しません。`--dry-run` はブロックを取得せず Tier 集計、上位20タグ、データベース別の行数を表示します。
ルート ID モードでは従来どおりネストしたページ・インライン DB を探索します。429/5xx・接続エラーを再試行し、
失敗ページは `selected.json` に記録して前回のベクトルを保持します。再帰ブロックは `data/cache/blocks/` に
キャッシュされ、必要なら `--no-block-cache` で無効化できます。

チャンクは見出しごとの小さなセクションを `CHUNK_TARGET_CHARS=1200` まで連結し、
`CHUNK_MAX_CHARS=1500` を超える長いセクションだけをオーバーラップ分割します。タイトル・プロパティも
最初の本文と結合されます。JSONL には `chunker_version` が入り、バージョン変更時は本文ブロックのキャッシュを
再利用して再チャンクします。各チャンクにはタイトルとタグの短い検索ヘッダーが付き、JSONL と
Vectorize／Chroma のメタデータにもタグ名（最大約200文字）が入ります。タグ以外の relation はIDを出力しません。

### 4. 索引化と検索

```bash
uv run python index_build.py
uv run python retriever.py "テスト質問"     # Slack を立てずに検索だけ確認できる
```

### 5. しきい値の校正（必須）

`SIM_THRESHOLD` の初期値 0.35 は暫定値。**実データで必ず校正する**。

```bash
cp eval/questions.tsv.example eval/questions.tsv
# 実 FAQ 10〜20問（in）と、Notion に答えが無い質問 5〜10問（out）を書く
uv run python eval_threshold.py
```

in-scope の最小値と out-of-scope の最大値の間に値を置き、`config.py` に反映する。
両者が重なる場合は低め寄りに取る（誤って「見つかりません」と返す方が害が小さい）。

### 6. 起動

```bash
uv run python bot.py
```

## 開発

```bash
uv run pytest          # chunking の決定性・Notion 変換・ACL のテスト
```

`chunking.py` は決定的でなければならない。壊れると「週次 cron のたびに全件が再埋め込みされる」
という気づきにくい形で表面化するため、テストで固定している。

Worker 版はしきい値を通ったヒットの前後チャンクも同じページから取得し、ページ単位に連結してから
回答モデルへ渡す（最大約 12,000 文字）。

## 運用

常駐（systemd）と週次更新（cron）の設定は [PLAN.md §15](PLAN.md)。

- systemd unit には **`User=` を必ず指定する**（省略すると root 常駐になる）
- `.env` は `600`、`data/` と `chroma_db/` は `700`（Notion 本文の平文コピーのため）
- `data/captions/` を消すと次回 cron で全画像が再キャプションされ課金が発生する。
  サーバー移設時は一緒に移すこと

## セキュリティ上の注意

**Notion のインテグレーショントークンには、Notion 上の人単位のアクセス権が反映されない。**
接続したページは Slack ワークスペース全員が Bot 経由で読めると考えること。
第一防衛線は Notion 側で「接続を追加」する対象を最小限に保つこと。

- 本リポジトリは **public**。`.env` `data/` `chroma_db/` `eval/questions.tsv` は `.gitignore` 済み。
  **push 前に `git show --stat` で内容を確認する**（public では誤コミットの取り消しが効かない）
- Notion 本文は信頼できない入力として扱う。プロンプトインジェクション対策は `llm_client.py`
- 画像キャプション化は新しい漏洩経路になる。画像内の文字が平文化され Slack から検索可能になる

公開前に [PLAN.md §17.9](PLAN.md) のチェックリストを実施すること。

## 鍵のローテーション

いずれも「新規発行 → `.env` 差し替え → `systemctl restart nutmeg-bot` → 旧キー失効」の順。

| 鍵 | 発行元 |
|---|---|
| `NOTION_TOKEN` | https://www.notion.so/my-integrations |
| `OPENAI_API_KEY` | https://platform.openai.com |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | https://api.slack.com/apps |
