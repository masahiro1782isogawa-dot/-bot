"""
習慣データを集計し、Google Gemini で AIフィードバックと名言を生成するモジュール。
"""

import json
import os
import time
from datetime import date, timedelta

from google import genai
from google.genai import types

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

RANK_THRESHOLDS = [
    (95, "S"),
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0,  "D"),
]


def _calc_score(habits: list[dict]) -> int:
    """達成率 (0-100) を返す。done=100%, skip=0% として平均。"""
    if not habits:
        return 0
    total_progress = sum(h["progress"] for h in habits)
    return round(total_progress / len(habits))


def _calc_streak(recent_days: list[dict]) -> int:
    """
    最新日から連続して done_ratio > 0.5 の日数を数える。
    recent_days は descending 順（新しい順）を想定。
    """
    streak = 0
    yesterday = date.today() - timedelta(days=1)

    for i, entry in enumerate(recent_days):
        expected = yesterday - timedelta(days=i)
        if entry["date"] != expected:
            break
        if entry["done_ratio"] >= 0.5:
            streak += 1
        else:
            break
    return streak


def _calc_weekly(recent_days: list[dict]) -> tuple[int, str]:
    """今週（月曜〜昨日）の週間スコアとランクを返す。"""
    yesterday = date.today() - timedelta(days=1)
    this_monday = yesterday - timedelta(days=yesterday.weekday())

    this_week = [
        e for e in recent_days
        if this_monday <= e["date"] <= yesterday
    ]

    if not this_week:
        return 0, "D"

    avg = sum(e["done_ratio"] for e in this_week) / len(this_week) * 100
    weekly_score = round(avg)
    rank = next(r for threshold, r in RANK_THRESHOLDS if weekly_score >= threshold)
    return weekly_score, rank


def _build_week_dots(recent_days: list[dict]) -> list[dict]:
    """
    今週月〜日の7ドット情報を返す。
    done_ratio >= 0.8 → 達成（緑）
    done_ratio >= 0.5 → 一部達成（黄）
    未来 → 白枠
    未記録（過去） → 灰色
    """
    yesterday = date.today() - timedelta(days=1)
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    recent_map = {e["date"]: e["done_ratio"] for e in recent_days}

    dots = []
    for i in range(7):
        d = monday + timedelta(days=i)
        label = WEEKDAY_JA[i]

        if d > yesterday:
            css = "bg-white border border-gray-200"
        else:
            ratio = recent_map.get(d)
            if ratio is None:
                css = "bg-gray-300"
            elif ratio >= 0.8:
                css = "bg-emerald-500"
            elif ratio >= 0.5:
                css = "bg-yellow-300"
            else:
                css = "bg-red-300"
        dots.append({"label": label, "css": css})

    return dots


def _format_date_ja(d: date) -> str:
    month = d.month
    day = d.day
    weekday = WEEKDAY_JA[d.weekday()]
    return f"{month}月{day}日({weekday})"


def _generate_ai_feedback(habits: list[dict], score: int, streak: int) -> dict:
    """Google Gemini でフィードバック JSON を生成して返す。"""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    habit_summary = "\n".join(
        f"- {h['emoji']} {h['name']}: {'✓完了' if h['status'] == 'done' else 'スキップ'} ({h['detail']})"
        for h in habits
    )
    done_count = sum(1 for h in habits if h["status"] == "done")
    total = len(habits)

    prompt = (
        "あなたは習慣コーチです。ユーザーの昨日の習慣達成状況を分析し、"
        "日本語で励ましと具体的なアドバイスを提供してください。"
        "回答は必ず以下のJSON形式のみで返してください（マークダウン・コードブロック不要）:\n"
        "{\n"
        '  "headline": "（達成状況を端的に表す1文、20文字以内）",\n'
        '  "points": ["（分析ポイント1文目、40文字以内）", "（分析ポイント2文目、40文字以内）"],\n'
        '  "action": "今日の action ▶ （具体的な1アクション、50文字以内）",\n'
        '  "quote": {"text": "（習慣・継続に関する名言）", "author": "（著者名）"}\n'
        "}\n\n"
        f"昨日の習慣記録:\n{habit_summary}\n\n"
        f"達成率: {score}%（{done_count}/{total}完了）\n"
        f"連続達成日数: {streak}日"
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.7,
                    max_output_tokens=600,
                    response_mime_type="application/json",
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            if attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"Gemini API エラー（{attempt + 1}回目）: {e}。{wait}秒後にリトライします...")
                time.sleep(wait)
            else:
                raise


def build_report_data(notion_data: dict, recent_days: list[dict]) -> dict:
    """
    Notionデータと集計情報からテンプレート用データ辞書を構築する。

    Returns:
        {
            "report": { date, greeting, score, streak, weekly_score, weekly_rank,
                        habits, feedback, quote },
            "done_count": int,
            "week_dots": [...],
        }
    """
    target_date: date = notion_data["target_date"]
    habits: list[dict] = notion_data["habits"]

    score = _calc_score(habits)
    streak = _calc_streak(recent_days)
    weekly_score, weekly_rank = _calc_weekly(recent_days)
    week_dots = _build_week_dots(recent_days)
    done_count = sum(1 for h in habits if h["status"] == "done")
    total = len(habits)

    ai = _generate_ai_feedback(habits, score, streak)

    greeting_map = {
        range(100, 101): "完璧な1日でした！素晴らしい！",
        range(80, 100):  "昨日の努力、お疲れ様でした！",
        range(60, 80):   "着実に前進しています。今日も続けましょう！",
        range(0, 60):    "完璧でなくて大丈夫。今日また一歩踏み出しましょう！",
    }
    greeting = "昨日の努力、お疲れ様でした！"
    for r, msg in greeting_map.items():
        if score in r:
            greeting = msg
            break

    return {
        "report": {
            "date": _format_date_ja(target_date),
            "greeting": greeting,
            "score": score,
            "streak": streak,
            "weekly_score": weekly_score,
            "weekly_rank": weekly_rank,
            "habits": habits,
            "feedback": {
                "headline": ai.get("headline", f"{done_count}/{total}達成！"),
                "points": ai.get("points", []),
                "action": ai.get("action", ""),
            },
            "quote": ai.get("quote", {"text": "", "author": ""}),
        },
        "done_count": done_count,
        "week_dots": week_dots,
    }
