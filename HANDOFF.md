# HANDOFF — nutmeg-bot

## 現在の状態
**Phase 1〜5 の実装が完了**。コードは全て書けており、鍵なしで到達できる検証は通っている。

- `uv run pytest` → **26 件成功**（chunking の決定性 / notion_md 変換 / bot の ACL）
- 全モジュールが鍵なしで import 可能。CLI の `--help` とエラーパスも確認済み
- git 初期化・remote 設定済み（<https://github.com/mtureit/nutmeg-bot>、**public**）。**未 push**
- **Notion / OpenAI / Slack への実接続はまだ一度も行っていない**（鍵が未発行のため）

## 次回やること（鍵の発行が最初のブロッカー）
1. **鍵を発行して `.env` を作る**（Notion / OpenAI / Slack、手順は PLAN.md §12）。
   テンプレートは README「2. 環境変数」。OpenAI は Usage limits で月額上限も設定
2. **NUTMEG 内で外部 API へのデータ移送の可否を確認**（Notion 本文・質問文・画像が OpenAI へ。§17.7）
3. **Notion 側で「接続を追加」する対象ページを決める**（取り込み範囲の第一防衛線。§17.1）
4. `prepare.py --dry-run` → `--no-images --limit 5` → `--limit 5` → 全体、と段階的に取り込む
5. `index_build.py` → `retriever.py "質問"` → `eval_threshold.py` で `SIM_THRESHOLD` 確定
6. Phase 7（systemd / cron）と §17.9 チェックリスト

## 実装上の申し送り
- **`PLAN.md` §5 から逸脱1点**: Notion ブロック→Markdown 変換を `notion_md.py` に分離した
  （ネットワーク非依存にしてテスト可能にするため）。PLAN.md §5 も更新済み
- **`.env.example` は未作成**。権限設定の deny ルールでブロックされたため、
  テンプレートは README「2. 環境変数」に置いてある
- `bot.py` の Bolt App は `build_app()` で遅延生成している。モジュール読み込み時に生成すると
  トークン未設定で import すら失敗するため

## 引き継ぐべき落とし穴
- **`SIM_THRESHOLD` は必ず実測校正する**。e5 前提の 0.75 を据え置くと全質問が「見つかりません」になる。
  暫定 0.35 → Phase 6 で `eval_threshold.py` により確定（手順は PLAN.md §11）
- **Chroma は距離を返す**。`similarity = 1 - distance` に変換してから比較する
- **Notion の画像 URL は約1時間で失効**する。JSONL に保存しても後から使えない。
  VLM へは base64、回答リンクは Notion ブロックディープリンク（PLAN.md §10.1）
- **キャプションキャッシュのキーは画像バイト列の sha256**。URL をキーにすると毎回キャッシュミスする。
  `data/captions/` を消すと週次 cron で全画像が再課金される
- 埋め込みモデル・`EMBED_DIM` を変えたら全再索引。`chroma_db/index_meta.json` で機械的に検出する
- **Notion のインテグレーショントークンには人単位のアクセス権が反映されない**。接続したページは
  Slack 全員に公開されると考える。取り込み範囲は `.env` の `NOTION_ROOT_PAGE_IDS` で明示し、
  `/v1/search` は使わない（PLAN.md §10.0・§17.1）
- **Notion 本文は信頼できない入力**。編集できる人が Bot の挙動を乗っ取れる（§17.2）
- **画像キャプション化は新しい漏洩経路**。画像内の文字が平文化され Slack から検索可能になる（§17.3）
- systemd unit に `User=` を入れ忘れると root 常駐になる（§15・§17.5）
- 公開前に **§17.9 のチェックリスト**を実施する

## 直近セッションの記録
- **2026-07-29（実装）**: Phase 1〜5 を実装。`.gitignore` を先に置いてから `git init` し
  remote を設定（push はユーザー）。`uv` が Python 3.12.10 を取得。
  `notion_md.py` を分離、`tests/` 3ファイル・`README.md`・`eval/questions.tsv.example` を追加。
  PLAN.md に §17.8 リポジトリ公開範囲を新設し、旧 §17.8 チェックリストを §17.9 に繰り下げ。
  検証: pytest 26 件成功、鍵なし import 成功、CLI ヘルプとエラーパス確認。
- **2026-07-29（後半）**: Notion 取得方式とセキュリティを追記。`PLAN.md` に **§10.0 取得対象ページの
  列挙**（ルートページ起点の再帰を採用、`/v1/search` は不採用）と **§17 セキュリティ設計**
  （脅威モデル A〜D、取り込み範囲の三重制御、プロンプトインジェクション、画像固有リスク、
  Slack アクセス制御、鍵とサーバー、ログ、外部送信範囲、公開前チェックリスト）を新設。
  §9 のシステムプロンプトに injection 対策を、§15 の systemd に `User=` と最小権限を追加。
  工数見積を 20〜31h → 22〜34h に更新。
- **2026-07-29**: 埋め込みを API 化する方針転換。ユーザーが3点を選択
  （埋め込み=OpenAI text-embedding-3-small / 生成も同一プロバイダに統一 / 画像=VLM キャプション化して索引）。
  `PLAN.md` を全面改訂（§1・§2・§4・§5・§6・§7・§8・§9・§10・§11・§12・§13・§14・§16・用語集）。
  新規モジュール `vision_client.py`（画像 DL・キャプション・キャッシュ）と `eval_threshold.py`
  （しきい値校正）を計画に追加。工数見積を 16〜26h → 20〜31h に更新。
  `CLAUDE.md` / `HANDOFF.md` も追随更新。
- **2026-07-18**: `NUTMEG_Slackbot_改訂第3版.pdf`（13p）を読み、Groq 前提でタスク分けを実施。
  `PLAN.md`（全 16 セクション）・`CLAUDE.md`・`HANDOFF.md` を新規作成。
  - 注: この PDF は現在作業ツリーに存在しない。PLAN.md が唯一の設計記録
  - 注意: git 操作はユーザーが実行。鍵は `.env` 管理で Git に含めない
