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
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | `.env` | Python から Vectorize に書き込む。トークンの権限は **Account › Vectorize › Edit** だけ |
| `NOTION_TOKEN` | `.env` | Notion の取り込み |

Worker の secret は値を画面に出さずに入れる:

```sh
cd worker
bunx wrangler secret put OPENAI_API_KEY
bunx wrangler secret put SLACK_BOT_TOKEN
bunx wrangler secret put SLACK_SIGNING_SECRET
```

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

- `waitUntil` は応答後 約30秒で打ち切られる。OpenAI の呼び出しは埋め込み 8 秒・生成 18 秒で打ち切る
- 回答1回あたりの外部リクエストは最大 6 回（users.info / 埋め込み / Vectorize query / getByIds / 生成 / chat.postMessage）。無料プランの上限 50 に収まる
- **Notion の本文が Cloudflare 上に置かれる**（Vectorize のメタデータとして。上限 10KiB/件、本文は 8000 バイトで切る）。
  ローカル完結だった Python 版からの変化なので、PLAN.md §17.7 の外部送信範囲に含めて考える
- 埋め込みモデルは Python（`config.py`）と Worker（`src/config.ts`）で**必ず同じにする**。変えたら全件再送
- `SIM_THRESHOLD` は Python 版と同じ暫定 0.35。Vectorize の cosine の score は類似度（大きいほど近い）
- Python の JSONL は `chunker_version` を持つ。チャンク設定を更新したときは、`prepare.py` がブロックキャッシュを
  再利用して再チャンクする。Worker はしきい値を通ったヒットの前後チャンクを `getByIds` で取得し、ページごとに
  重複除去・文書順で連結してから回答する。
