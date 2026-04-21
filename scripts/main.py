"""
習慣内容自動報告分析ツール エントリーポイント。

実行順:
  1. Notion から昨日の習慣データを取得
  2. 集計・AI フィードバック生成
  3. HTML → PNG レンダリング
  4. Slack へ投稿
  5. 一時 PNG ファイルを削除（finally で保証）
"""

import logging
import os
import sys

from fetch_notion import fetch_yesterday_habits, fetch_recent_days
from generate_report import build_report_data
from render_image import render_report_image
from send_slack import send_report_image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# レポート生成〜Slack 投稿まで一連で必須（未設定だと途中で分かりにくいエラーになるのを防ぐ）
_REQUIRED_ENV_VARS = (
    "NOTION_API_KEY",
    "NOTION_DATABASE_ID",
    "GEMINI_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
)


def _require_env_vars() -> None:
    missing = [
        name
        for name in _REQUIRED_ENV_VARS
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        logger.error(
            "必須の環境変数が設定されていません: %s\n"
            "GitHub Actions の Secrets、またはローカルでの export を確認してください。",
            ", ".join(missing),
        )
        raise SystemExit(1)


def main() -> None:
    logger.info("=== 習慣レポート生成開始 ===")
    _require_env_vars()

    try:
        logger.info("Step 1: Notion からデータ取得中...")
        notion_data = fetch_yesterday_habits()
        recent_days = fetch_recent_days(n=30)
        logger.info(
            "取得完了: %s (%d 件の習慣)",
            notion_data["target_date"],
            len(notion_data["habits"]),
        )

        logger.info("Step 2: 集計 & AI フィードバック生成中...")
        report_data = build_report_data(notion_data, recent_days)
        logger.info(
            "達成率: %d%%, 連続: %d日, 週間スコア: %d (%s)",
            report_data["report"]["score"],
            report_data["report"]["streak"],
            report_data["report"]["weekly_score"],
            report_data["report"]["weekly_rank"],
        )

        logger.info("Step 3: PNG 画像レンダリング中...")
        image_path = render_report_image(report_data)
        logger.info("画像生成完了: %s", image_path)

        logger.info("Step 4: Slack へ投稿中...")
        try:
            permalink = send_report_image(image_path, notion_data["target_date"])
            logger.info("投稿完了: %s", permalink)
        finally:
            # Slack 投稿の成否にかかわらず一時 PNG を削除する
            if os.path.exists(image_path):
                os.unlink(image_path)
                logger.info("一時 PNG ファイルを削除しました: %s", image_path)

    except ValueError as e:
        # Notion にレコードが存在しない日（習慣未記録）
        logger.error("データ取得エラー: %s", e)
        raise SystemExit(1) from e
    except Exception as e:
        logger.exception("予期しないエラーが発生しました: %s", e)
        raise SystemExit(1) from e

    logger.info("=== 習慣レポート生成完了 ===")


if __name__ == "__main__":
    main()
