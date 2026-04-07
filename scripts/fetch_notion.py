"""
Notion データベースから昨日の習慣記録を取得するモジュール。

Notionデータベース列構造（推奨）:
  日付         — Date
  筋トレ        — Checkbox
  筋トレ詳細    — Rich Text
  ジャーナル    — Checkbox
  ジャーナル詳細 — Rich Text
  瞑想          — Checkbox
  瞑想詳細      — Rich Text
  勉強          — Checkbox
  勉強詳細      — Rich Text
  イメージング  — Checkbox
  イメージング詳細 — Rich Text
"""

import os
from datetime import date, timedelta
from notion_client import Client

HABIT_DEFINITIONS = [
    {"emoji": "💪", "name": "筋トレ"},
    {"emoji": "📓", "name": "ジャーナル"},
    {"emoji": "🧘", "name": "瞑想"},
    {"emoji": "📚", "name": "勉強"},
    {"emoji": "🌟", "name": "イメージング"},
]


def _extract_text(prop: dict) -> str:
    """Rich Text プロパティから文字列を取り出す。"""
    items = prop.get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in items).strip()


def _extract_checkbox(prop: dict) -> bool:
    return prop.get("checkbox", False)


def fetch_yesterday_habits() -> dict:
    """
    昨日の日付でNotionデータベースをクエリし、習慣データを返す。

    Returns:
        {
            "target_date": date,
            "habits": [
                {
                    "emoji": str,
                    "name": str,
                    "detail": str,
                    "status": "done" | "skip",
                    "progress": int,  # done=100, skip=0（詳細テキストから数値があれば上書き可）
                },
                ...
            ],
            "raw_page": dict,  # Notion ページオブジェクト（週間集計用）
        }
    """
    notion = Client(auth=os.environ["NOTION_API_KEY"])
    db_id = os.environ["NOTION_DATABASE_ID"]

    yesterday = date.today() - timedelta(days=1)
    iso = yesterday.isoformat()

    response = notion.databases.query(
        database_id=db_id,
        filter={
            "property": "日付",
            "date": {
                "equals": iso,
            },
        },
    )

    pages = response.get("results", [])
    if not pages:
        raise ValueError(f"Notionに {iso} のレコードが見つかりませんでした。前日に習慣を記録してください。")

    page = pages[0]
    props = page["properties"]

    habits = []
    for hd in HABIT_DEFINITIONS:
        name = hd["name"]
        done = _extract_checkbox(props.get(name, {}))
        detail_raw = _extract_text(props.get(f"{name}詳細", {}))
        detail = detail_raw if detail_raw else ("未実施" if not done else "")

        # progress: done なら 100、detail に "XX%" 形式があれば上書き
        progress = 100 if done else 0
        for token in detail.split():
            if token.endswith("%") and token[:-1].isdigit():
                progress = int(token[:-1])
                break

        habits.append(
            {
                "emoji": hd["emoji"],
                "name": name,
                "detail": detail,
                "status": "done" if done else "skip",
                "progress": progress,
            }
        )

    return {
        "target_date": yesterday,
        "habits": habits,
        "raw_page": page,
    }


def fetch_recent_days(n: int = 30) -> list[dict]:
    """
    直近 n 日分の習慣達成データを取得する（ストリーク・週間スコア計算用）。

    Returns:
        [{"date": date, "done_ratio": float}, ...]  新しい日付順
    """
    notion = Client(auth=os.environ["NOTION_API_KEY"])
    db_id = os.environ["NOTION_DATABASE_ID"]

    since = (date.today() - timedelta(days=n)).isoformat()

    response = notion.databases.query(
        database_id=db_id,
        filter={
            "property": "日付",
            "date": {"on_or_after": since},
        },
        sorts=[{"property": "日付", "direction": "descending"}],
    )

    results = []
    for page in response.get("results", []):
        props = page["properties"]
        date_val = props.get("日付", {}).get("date", {})
        if not date_val or not date_val.get("start"):
            continue

        page_date = date.fromisoformat(date_val["start"])
        done_count = sum(
            1
            for hd in HABIT_DEFINITIONS
            if _extract_checkbox(props.get(hd["name"], {}))
        )
        total = len(HABIT_DEFINITIONS)
        results.append(
            {
                "date": page_date,
                "done_ratio": done_count / total if total else 0,
            }
        )

    return results
