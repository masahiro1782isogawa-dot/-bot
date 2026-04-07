"""
Slack へ習慣レポート画像を投稿するモジュール。
"""

import os
from datetime import date
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def send_report_image(image_path: str, report_date: date) -> str:
    """
    Slack チャンネルに PNG 画像をアップロードして投稿する。

    Args:
        image_path: ローカルの PNG ファイルパス
        report_date: レポート対象日（メッセージテキストに使用）
    Returns:
        アップロードされたファイルの URL (permalink)
    Raises:
        SlackApiError: Slack API のエラー
    """
    token = os.environ["SLACK_BOT_TOKEN"]
    channel_id = os.environ["SLACK_CHANNEL_ID"]

    client = WebClient(token=token)

    month = report_date.month
    day = report_date.day
    initial_comment = (
        f":sunrise: *{month}月{day}日の習慣レポート* が届きました！\n"
        "今日も一歩ずつ積み上げていきましょう :muscle:"
    )

    try:
        response = client.files_upload_v2(
            channel=channel_id,
            file=image_path,
            filename=f"habit_report_{report_date.isoformat()}.png",
            title=f"習慣レポート {month}月{day}日",
            initial_comment=initial_comment,
        )
    except SlackApiError as e:
        raise SlackApiError(
            f"Slack への画像投稿に失敗しました: {e.response['error']}", e.response
        ) from e

    files = response.get("files", [])
    if files:
        return files[0].get("permalink", "")
    return ""
