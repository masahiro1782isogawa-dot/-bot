"""
習慣データを集計し、レポートデータを構築するモジュール。

AI（Gemini）は「コーチングコメント（points + action）」の生成のみに使用する。
headline・greeting・score・streak・quote はプログラムで決定論的に生成し、出力を安定させる。

スコア計算ルール:
  - 1習慣 = 20点（5習慣 × 20点 = 100点満点）
  - 達成(done) → 20点 / スキップ(skip) → 0点
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
from google import genai
from google.genai import types

# GitHub Actions は UTC 動作のため、JST (UTC+9) で日付を計算する
_JST = timezone(timedelta(hours=9))

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

# ランク閾値（100点満点基準）
RANK_THRESHOLDS = [
    (95, "S"),
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0,  "D"),
]

POINTS_PER_HABIT = 20  # 1習慣あたりの点数

# ──────────────────────────────────────────────
# フォールバック用の日本語名言リスト（31件）
# ZenQuotes API が失敗した場合に day-of-month で選択する
# ──────────────────────────────────────────────
_FALLBACK_QUOTES = [
    {"text": "千里の道も一歩から。", "author": "老子"},
    {"text": "私たちは繰り返し行うことの産物である。優秀さとは行為ではなく、習慣なのだ。", "author": "アリストテレス"},
    {"text": "行動が常に幸福をもたらすわけではないが、行動のないところに幸福はない。", "author": "ベンジャミン・ディズレーリ"},
    {"text": "夢を持ち続けなさい。そして、毎日その夢に一歩近づく何かをしなさい。", "author": "ウォルト・ディズニー"},
    {"text": "今日できることを明日に延ばすな。", "author": "ベンジャミン・フランクリン"},
    {"text": "努力は必ず報われる。もし報われない努力があるとすれば、それはまだ努力とは呼べない。", "author": "王貞治"},
    {"text": "成功とは、失敗を重ねても熱意を失わないでいられる能力である。", "author": "ウィンストン・チャーチル"},
    {"text": "明日やろうはバカやろう。", "author": "岡本太郎"},
    {"text": "継続は力なり。", "author": "ことわざ"},
    {"text": "人生は自転車に乗るようなもの。バランスを保つにはとにかく動き続けなければならない。", "author": "アルベルト・アインシュタイン"},
    {"text": "あなたが諦めない限り、失敗はない。", "author": "ナポレオン・ヒル"},
    {"text": "最大の名誉は決して倒れないことではなく、倒れるたびに起き上がることにある。", "author": "孔子"},
    {"text": "小さいことを重ねることが、とんでもないところへ行くただ一つの道だ。", "author": "イチロー"},
    {"text": "勝負はすでに練習のときについている。", "author": "野村克也"},
    {"text": "才能とは、努力を続けられる能力のことだ。", "author": "サミュエル・ジョンソン"},
    {"text": "困難の中に機会がある。", "author": "アルベルト・アインシュタイン"},
    {"text": "できるかどうかではなく、やるかどうかだ。", "author": "ヨーダ（スター・ウォーズ）"},
    {"text": "あなたの時間は限られている。他の誰かの人生を生きることで時間を無駄にするな。", "author": "スティーブ・ジョブズ"},
    {"text": "一日一日を大切に。全ての今日が、昨日夢見ていた明日だったのだから。", "author": "ニコラス・カタネオ"},
    {"text": "進歩のない者は必ず退歩する。", "author": "新渡戸稲造"},
    {"text": "石の上にも三年。", "author": "ことわざ"},
    {"text": "自分を信じること。それが成功への最初の秘訣だ。", "author": "ラルフ・ワルド・エマーソン"},
    {"text": "今日という日は、残りの人生の最初の日だ。", "author": "チャールズ・ディードリッヒ"},
    {"text": "できると思えばできる、できないと思えばできない。これは揺るぎない法則だ。", "author": "ヘンリー・フォード"},
    {"text": "人は習慣によって形作られる。卓越さとは行為ではなく習慣だ。", "author": "ウィル・デュラント"},
    {"text": "昨日のベストが、今日のスタートライン。", "author": "柔道の格言"},
    {"text": "鉄は熱いうちに打て。", "author": "ことわざ"},
    {"text": "やってみせ、言って聞かせて、させてみせ、ほめてやらねば人は動かじ。", "author": "山本五十六"},
    {"text": "失敗することを恐れるより、何もしないことを恐れろ。", "author": "本田宗一郎"},
    {"text": "七転び八起き。", "author": "ことわざ"},
    {"text": "過去を変えることはできないが、未来は自分で作ることができる。", "author": "ジョン・C・マクスウェル"},
]


def _today_jst() -> date:
    """JST での今日の日付を返す。"""
    return datetime.now(_JST).date()


# ──────────────────────────────────────────────
# スコア計算（5習慣 × 20点 = 100点満点）
# ──────────────────────────────────────────────

def _calc_score(habits: list[dict]) -> int:
    """
    1日の達成スコアを返す（0–100）。
    done=20点 / skip=0点。5習慣フル達成で100点。
    """
    done_count = sum(1 for h in habits if h["status"] == "done")
    return done_count * POINTS_PER_HABIT


def _calc_streak(recent_days: list[dict]) -> int:
    """
    Notionの習慣記録から連続達成日数を計算する。

    日付辞書（date → done_ratio）を作り、昨日から1日ずつ遡って
    記録が存在しかつ done_ratio >= 0.5 の日数をカウントする。
    記録が存在しない日（Notionに入力なし）はストリーク終了とみなす。

    Args:
        recent_days: fetch_recent_days() の戻り値（降順ソート済み）
    """
    date_map: dict[date, float] = {e["date"]: e["done_ratio"] for e in recent_days}

    streak = 0
    yesterday = _today_jst() - timedelta(days=1)

    for i in range(len(recent_days) + 1):
        check_date = yesterday - timedelta(days=i)
        ratio = date_map.get(check_date)
        if ratio is None:
            # Notionに記録がない日 = ストリーク終了
            break
        if ratio >= 0.5:
            streak += 1
        else:
            # 記録はあるが達成率50%未満 = ストリーク終了
            break

    return streak


def _calc_weekly(recent_days: list[dict]) -> tuple[int, str]:
    """
    今週（月曜〜昨日）の週間スコアとランクを返す。
    週間スコア = 各日の達成習慣数 × 20点 の平均。
    """
    yesterday = _today_jst() - timedelta(days=1)
    this_monday = yesterday - timedelta(days=yesterday.weekday())

    this_week = [
        e for e in recent_days
        if this_monday <= e["date"] <= yesterday
    ]

    if not this_week:
        return 0, "D"

    # done_ratio = done_count / 5 なので × 100 = done_count × 20（20点制と等価）
    avg = sum(e["done_ratio"] for e in this_week) / len(this_week) * 100
    weekly_score = round(avg)
    rank = next(r for threshold, r in RANK_THRESHOLDS if weekly_score >= threshold)
    return weekly_score, rank


def _build_week_dots(recent_days: list[dict]) -> list[dict]:
    """
    今週月〜日の7ドット情報を返す。
    done_ratio >= 0.8 → 達成（緑）
    done_ratio >= 0.5 → 一部達成（黄）
    未来           → 白枠
    未記録（過去）  → 灰色
    """
    yesterday = _today_jst() - timedelta(days=1)
    today = _today_jst()
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
    return f"{d.month}月{d.day}日({WEEKDAY_JA[d.weekday()]})"


# ──────────────────────────────────────────────
# プログラムで決定論的に生成する箇所
# ──────────────────────────────────────────────

def _build_headline(done_count: int, total: int, score: int) -> str:
    """スコアと達成数から見出しを生成する（AI不要）。"""
    if score == 100:
        return f"完璧！{done_count}/{total}全て達成しました！"
    if score >= 80:
        return f"{done_count}/{total}達成！着実に積み上がっています"
    if score >= 60:
        return f"{done_count}/{total}達成。もう一息です！"
    if done_count > 0:
        return f"{done_count}/{total}達成。小さな一歩が大切です"
    return "今日からまた積み上げていきましょう"


def _build_greeting(score: int) -> str:
    """達成率からグリーティングを生成する（AI不要）。"""
    if score == 100:
        return "完璧な1日でした！素晴らしい！"
    if score >= 80:
        return "昨日の努力、お疲れ様でした！"
    if score >= 60:
        return "着実に前進しています。今日も続けましょう！"
    return "完璧でなくて大丈夫。今日また一歩踏み出しましょう！"


def _fetch_quote_from_web(target_date: date) -> dict:
    """
    ZenQuotes API（https://zenquotes.io）からその日の名言を取得する。
    API が失敗した場合は日付ベースでフォールバックリストから選択する。

    ZenQuotes は 1日1回同じ名言を返す（/api/today）ため毎日確実に異なる。
    """
    try:
        resp = requests.get(
            "https://zenquotes.io/api/today",
            timeout=10,
            headers={"User-Agent": "HabitReportBot/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list) and data[0].get("q") and data[0].get("a"):
            q = data[0]["q"].strip()
            a = data[0]["a"].strip()
            print(f"ZenQuotes API 成功: {a}")
            return {"text": q, "author": a}
    except Exception as e:
        print(f"ZenQuotes API 失敗（{e}）。フォールバックリストを使用します。")

    # フォールバック: 日付の通し番号でリストを循環（毎日異なる名言）
    idx = (target_date.toordinal()) % len(_FALLBACK_QUOTES)
    return _FALLBACK_QUOTES[idx]


# ──────────────────────────────────────────────
# AI（Gemini）を使う箇所: points + action のみ
# ──────────────────────────────────────────────

def _generate_ai_coaching(habits: list[dict], score: int, streak: int) -> dict:
    """
    Google Gemini で「分析ポイント2文」と「今日の1アクション」だけを生成する。
    失敗時はプログラムで生成した静的フォールバックを返す。
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    done_names = [h["name"] for h in habits if h["status"] == "done"]
    skip_names = [h["name"] for h in habits if h["status"] == "skip"]
    habit_summary = "\n".join(
        f"- {h['emoji']} {h['name']}: {'✓完了' if h['status'] == 'done' else 'スキップ'} ({h['detail']})"
        for h in habits
    )
    done_count = len(done_names)
    total = len(habits)

    prompt = (
        "あなたは習慣コーチです。ユーザーの昨日の習慣達成状況を分析してください。\n"
        "回答は必ず以下のJSON形式のみで返してください（マークダウン・コードブロック不要）:\n"
        "{\n"
        '  "points": ["（達成した習慣への具体的な気づき、40文字以内）", "（スキップした習慣への優しいアドバイス、40文字以内）"],\n'
        '  "action": "今日の action ▶ （明日への具体的な1アクション、50文字以内）"\n'
        "}\n\n"
        f"昨日の習慣記録:\n{habit_summary}\n\n"
        f"スコア: {score}点/100点（{done_count}/{total}達成）、連続達成日数: {streak}日"
    )

    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]

    for model in models:
        for attempt in range(3):
            try:
                print(f"Gemini API 呼び出し中（モデル: {model}, 試行: {attempt + 1}/3）...")
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.5,
                        max_output_tokens=300,
                        response_mime_type="application/json",
                    ),
                )
                result = json.loads(response.text)
                if "points" in result and "action" in result:
                    print(f"Gemini API 成功（モデル: {model}）")
                    return result
                print("Gemini API: 必須キー不足。フォールバックへ")
                break
            except Exception as e:
                if attempt < 2:
                    wait = 10 * (2 ** attempt)
                    print(f"Gemini API エラー（{model}, {attempt + 1}回目）: {e}。{wait}秒後にリトライ...")
                    time.sleep(wait)
                else:
                    print(f"Gemini API 失敗（モデル: {model} 全試行終了）: {e}")
                    break

    # 全モデル失敗時のフォールバック（プログラムで生成）
    print("警告: Gemini API が全モデルで失敗しました。静的コーチングで続行します。")
    skip_text = f"{skip_names[0]}に再チャレンジしてみましょう。" if skip_names else "今日も習慣を続けましょう。"
    done_text = f"{done_names[0]}の達成が今日の自信につながります。" if done_names else "小さな一歩が積み重なっていきます。"
    return {
        "points": [
            f"スコア{score}点、連続{streak}日継続中です。",
            done_text,
        ],
        "action": f"今日の action ▶ {skip_text}",
    }


# ──────────────────────────────────────────────
# メイン組み立て関数
# ──────────────────────────────────────────────

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

    # プログラムで決定論的に生成（AI不要）
    headline = _build_headline(done_count, len(habits), score)
    greeting = _build_greeting(score)
    quote = _fetch_quote_from_web(target_date)  # Web取得 + フォールバックリスト

    # AI はコーチングコメント（points + action）のみ
    coaching = _generate_ai_coaching(habits, score, streak)

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
                "headline": headline,
                "points": coaching.get("points", []),
                "action": coaching.get("action", ""),
            },
            "quote": quote,
        },
        "done_count": done_count,
        "week_dots": week_dots,
    }
