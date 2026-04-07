"""
習慣内容自動報告分析ツール エントリーポイント。

実行順:
  1. Notion から昨日の習慣データを取得
  2. 集計・AI フィードバック生成
  3. HTML → PNG レンダリング
  4. Slack へ投稿
"""

import logging
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


def main() -> None:
    logger.info("=== 習慣レポート生成開始 ===")

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
    permalink = send_report_image(image_path, notion_data["target_date"])
    logger.info("投稿完了: %s", permalink)

    logger.info("=== 習慣レポート生成完了 ===")


if __name__ == "__main__":
    main()
