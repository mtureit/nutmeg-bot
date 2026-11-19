# nutmeg-bot Worker（Cloudflare Workers 版）

Slack のメンションに、Notion の内容から答える Bot の **回答側**。
取り込み（Notion → 索引）は Python のまま残し、索引だけを Cloudflare Vectorize に置く。

```
【取り込み】Python（手元の Mac。のちに GitHub Actions で週1回）
  Notion ─▶ prepare.py ─▶ index_build.py --target vectorize ──▶ Vectorize "nutmeg-bot-512"

【回答】この Worker（https://nutmeg-bot.mtureit.workers.dev）
  Slack @メンション ─POST /slack/events─▶ ① 署名検証 ② 重複排除(KV) ③ 即 200
     waitUntil の中で: ゲスト判定 → 質問を埋め込み → Vectorize 検索・前後チャンク展開
                       → ページ単位に連結（最大約12,000文字）→ OpenAI で回答 → スレッドに返信（出典つき）
```

Python 版（Socket Mode + Chroma）は branch `python-socket-mode` に凍結してある。

## 置き場所

| もの | 場所 |
|---|---|
| Cloudflare アカウント | **Mtureit@gmail.com's Account**（`4a114bfdd139e285794ff69108c5c5f9`）。`wrangler.jsonc` の `account_id` で固定 |
| Worker | `nutmeg-bot` → <https://nutmeg-bot.mtureit.workers.dev> |
| Vectorize | `nutmeg-bot-512`（512 次元 / cosine）。既存の `nutmeg-bot` は変更しない |
| KV | `nutmeg-bot-dedup`（`076d1f28…`）。Slack の再送を弾く |

## 鍵

| 鍵 | どこに | 用途 |
|---|---|---|
| `OPENAI_API_KEY` | Worker の secret と `.env` | 質問の埋め込み・回答（Worker）／チャンクの埋め込み（Python） |
| `SLACK_BOT_TOKEN`（`xoxb-`） | Worker の secret | 返信・ゲスト判定 |
| `SLACK_SIGNING_SECRET` | Worker の secret | Slack からのリクエストの署名検証 |
| `NOTION_TOKEN` | Worker の secret（任意） | 索引外 fallback、タグ/最近コマンド、関連ページ |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | `.env` | Python から Vectorize に書き込む。トークンの権限は **Account › Vectorize › Edit** だけ |
| `NOTION_TOKEN` | `.env` | Notion の取り込み |

Worker の secret は値を画面に出さずに入れる:

```sh
cd worker
bunx wrangler secret put OPENAI_API_KEY
bunx wrangler secret put SLACK_BOT_TOKEN
bunx wrangler secret put SLACK_SIGNING_SECRET
# Notion integration は対象ページだけの read-only 権限にする
pbpaste | bunx wrangler secret put NOTION_TOKEN
```

`NOTION_TOKEN` が未設定でも、既存の Vectorize 索引を使う通常質問は動作する。Notion のライブ fallback とコマンドは利用不可になる。

## Notion コマンドと索引外 fallback

メンション後の質問が次のコマンドに一致する場合、LLM を呼ばずに Notion API から一覧を返す。

```text
タグ一覧       # 記事数の多い順に最大30タグ
タグ <name>    # そのタグの新しい記事を最大10件
最近           # 最終編集が新しい記事を最大10件
ヘルプ / help
```

タグの件数とタグ一覧は KV の `cache:tagcounts:v2` / `cache:taglib:v2` に24時間保存する。同じ名前（前後空白を除き、大文字小文字を区別しない）のタグ行は1グループにまとめ、複数のページ ID を使って記事を検索する。KV が空で時間が足りない場合は、タグ名だけを返して件数が unavailable であることを示す。個人ページなどの除外 ID は、一覧・fallback・関連ページのすべてで適用する。

Vectorize にしきい値以上の文脈がない場合、または索引の LLM 回答が `found=false`（Not Found）と判定された場合、`NOTION_TOKEN` があれば質問からタイトル検索語を作り、Notion の検索結果を祖先ページまで確認して最大2ページを直接取得する。索引の回答中はこの取得を先行して開始し、索引で回答できた場合はキャンセルして結果を捨てる。本文は対応するブロックだけを Markdown 化し、伏字化してから各ページ6,000文字に切る。出典には `（索引外・Notion から直接取得）` を付ける。

索引から回答できた場合は、引用ページの中で記事数が最も少ないタグを使い、関連ページを最大3件付ける。ライブ fallback と Not Found の回答には関連ページを付けない。

## セットアップ

### 1. 索引を Vectorize に入れる

```sh
cd worker
bunx wrangler vectorize create nutmeg-bot-512 --dimensions=512 --metric=cosine
cd ..
uv run python prepare.py --no-images          # まだなら
uv run python index_build.py --target vectorize
```

前回送った ID を `data/vectorize_ids.json` に index 名付きで残し、別 index の manifest や旧形式の
manifest は削除候補に使わない。消えたチャンクは同じ index からも削除する。
Vectorize への反映は非同期で、数十秒かかる。

### 2. Slack アプリ（HTTP 方式）

<https://api.slack.com/apps> → Create New App → From a manifest:

```yaml
display_information:
  name: NUTMEG Bot
features:
  bot_user:
    display_name: NUTMEG Bot
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - users:read
settings:
  event_subscriptions:
    request_url: https://nutmeg-bot.mtureit.workers.dev/slack/events
    bot_events:
      - app_mention
  socket_mode_enabled: false
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  token_rotation_enabled: false
```

1. Basic Information の **Signing Secret** → `wrangler secret put SLACK_SIGNING_SECRET`
2. Install App → Bot User OAuth Token（`xoxb-`）→ `wrangler secret put SLACK_BOT_TOKEN`
3. Event Subscriptions で Request URL が **Verified** になるのを確認する
   （signing secret を入れる前は検証に失敗する。入れてから「Retry」を押す）
4. 使うチャンネルで `/invite @NUTMEG Bot` → メンションして質問する

### 3. 開発

```sh
cd worker
bun install
bun test               # Slack・OpenAI・Vectorize をモックする。鍵は不要
bun run check:worker   # 型チェック
bunx wrangler deploy   # mtureit アカウントへ
bunx wrangler tail     # 本番ログ（質問文は出さない）
```

## 制限と注意

- `waitUntil` は応答後 約30秒で打ち切られる。処理全体はイベント開始から約27秒を期限とし、Slack 返信用の予約時間を残す。残り12秒未満ならライブ fallback を省略する。索引回答後のライブ再回答には5秒以上を確保できる場合だけ2回目の生成を行う。OpenAI の呼び出しは埋め込み8秒・生成18秒を上限に、最終回答には残り時間を渡す
- 回答1回あたりの外部リクエストは最大 6 回（users.info / 埋め込み / Vectorize query / getByIds / 生成 / chat.postMessage）。無料プランの上限 50 に収まる
- **Notion の本文が Cloudflare 上に置かれる**（Vectorize のメタデータとして。上限 10KiB/件、本文は 8000 バイトで切る）。
  ローカル完結だった Python 版からの変化なので、PLAN.md §17.7 の外部送信範囲に含めて考える
- 埋め込みモデルは Python（`config.py`）と Worker（`src/config.ts`）で**必ず同じにする**。変えたら全件再送
- `SIM_THRESHOLD` は Python 版と同じ暫定 0.35。Vectorize の cosine の score は類似度（大きいほど近い）
- Python の JSONL は `chunker_version` を持つ。チャンク設定を更新したときは、`prepare.py` がブロックキャッシュを
  再利用して再チャンクする。Worker はしきい値を通ったヒットの前後チャンクを `getByIds` で取得し、ページごとに
  重複除去・文書順で連結してから回答する。
