"""
Notionに習慣トラッカーデータベースを自動作成するセットアップスクリプト。
実行すると正しい列構造のデータベースが作成され、Database IDが表示されます。
"""

import os
import sys
from notion_client import Client


def create_habit_database(api_key: str, page_id: str) -> str:
    notion = Client(auth=api_key)

    # ハイフンなしIDをハイフンあり形式に変換
    pid = page_id.replace("-", "")
    formatted = f"{pid[:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:]}"

    print("Notionにデータベースを作成中...")

    response = notion.databases.create(
        parent={"type": "page_id", "page_id": formatted},
        title=[{"type": "text", "text": {"content": "習慣トラッカーDB"}}],
        properties={
            "日付": {"date": {}},
            "筋トレ": {"checkbox": {}},
            "筋トレ詳細": {"rich_text": {}},
            "ジャーナル": {"checkbox": {}},
            "ジャーナル詳細": {"rich_text": {}},
            "瞑想": {"checkbox": {}},
            "瞑想詳細": {"rich_text": {}},
            "勉強": {"checkbox": {}},
            "勉強詳細": {"rich_text": {}},
            "イメージング": {"checkbox": {}},
            "イメージング詳細": {"rich_text": {}},
        },
    )

    db_id = response["id"]
    print()
    print("=" * 60)
    print("データベース作成完了！")
    print(f"NOTION_DATABASE_ID = {db_id}")
    print("=" * 60)
    print()
    print("上のIDをコピーしてCursorに貼り付けてください。")
    return db_id


if __name__ == "__main__":
    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        print("エラー: NOTION_API_KEY 環境変数が設定されていません。")
        sys.exit(1)

    page_id = "33bd3c6cf76e80c98279cd69e518a84a"
    create_habit_database(api_key, page_id)
