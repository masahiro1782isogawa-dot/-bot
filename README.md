# 習慣内容自動報告分析ツール

毎朝7:00に Notion の習慣データベースを取得し、AIフィードバック付きのレポートカード画像を Slack へ自動投稿するツールです。

## アーキテクチャ

```
Notion DB (習慣記録)
    ↓ Notion API
GitHub Actions (毎朝7:00 JST)
    ↓ fetch → 集計 → OpenAI → Playwright (PNG) → Slack
Slack チャンネル
```

## セットアップ手順

### 1. Notion データベースを作成する

以下の列構造でデータベースを作成してください（1行 = 1日分）:

| 列名 | プロパティ種別 | 説明 |
|------|--------------|------|
| 日付 | Date | クエリのキー。毎日入力 |
| 筋トレ | Checkbox | 達成したらチェック |
| 筋トレ詳細 | Rich Text | 例: "胸・肩 / 45分" |
| ジャーナル | Checkbox | |
| ジャーナル詳細 | Rich Text | 例: "感謝・目標・気づき / 20分" |
| 瞑想 | Checkbox | |
| 瞑想詳細 | Rich Text | 例: "マインドフルネス / 15分" |
| 勉強 | Checkbox | |
| 勉強詳細 | Rich Text | 例: "マーケティング / 60分" |
| イメージング | Checkbox | |
| イメージング詳細 | Rich Text | |

> 習慣を追加・変更する場合は `scripts/fetch_notion.py` の `HABIT_DEFINITIONS` リストを編集してください。

### 2. Notion Integration を作成する

1. https://www.notion.so/my-integrations にアクセス
2. 「新しいインテグレーション」を作成
3. 発行された **Internal Integration Token** をメモする（`NOTION_API_KEY`）
4. 習慣データベースのページを開き、右上「…」→「コネクト」から作成したインテグレーションを追加
5. データベースの URL から ID を取得する（`NOTION_DATABASE_ID`）
   - URL 例: `https://www.notion.so/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...`
   - `?v=` の前の 32文字が Database ID

### 3. OpenAI API キーを取得する

1. https://platform.openai.com/api-keys でキーを発行
2. `OPENAI_API_KEY` としてメモ

### 4. Slack Bot を作成する

1. https://api.slack.com/apps で新しいアプリを作成
2. 「OAuth & Permissions」→ Bot Token Scopes に以下を追加:
   - `files:write`
   - `chat:write`
3. ワークスペースにインストールし、**Bot User OAuth Token** をメモ（`SLACK_BOT_TOKEN`）
4. 投稿先チャンネルにボットを招待し、チャンネル ID をメモ（`SLACK_CHANNEL_ID`）
   - チャンネルを右クリック →「チャンネル詳細を表示」→ 一番下に表示される `C...` の ID

### 5. GitHub Secrets を登録する

GitHubリポジトリの「Settings → Secrets and variables → Actions」に以下を登録:

| Secret 名 | 値 |
|-----------|---|
| `NOTION_API_KEY` | Notion Integration Token |
| `NOTION_DATABASE_ID` | Notion データベース ID |
| `OPENAI_API_KEY` | OpenAI API キー |
| `SLACK_BOT_TOKEN` | Slack Bot Token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | Slack チャンネル ID (`C...`) |

### 6. 動作確認（手動実行）

GitHubリポジトリの「Actions」タブ → 「習慣レポート自動送信」→「Run workflow」で手動実行できます。

## ファイル構成

```
.
├── .github/
│   └── workflows/
│       └── daily_report.yml    # GitHub Actions ワークフロー
├── scripts/
│   ├── main.py                 # エントリーポイント
│   ├── fetch_notion.py         # Notion APIクライアント
│   ├── generate_report.py      # 集計 & OpenAI フィードバック生成
│   ├── render_image.py         # HTML → PNG レンダリング
│   └── send_slack.py           # Slack 投稿
├── templates/
│   └── report.html             # Jinja2 レポートテンプレート
├── requirements.txt
└── README.md
```

## 習慣のカスタマイズ

`scripts/fetch_notion.py` の `HABIT_DEFINITIONS` を編集することで習慣項目を変更できます:

```python
HABIT_DEFINITIONS = [
    {"emoji": "💪", "name": "筋トレ"},
    {"emoji": "📓", "name": "ジャーナル"},
    # ここに追加・変更
]
```

Notion DB の列名と `name` フィールドを一致させてください。
