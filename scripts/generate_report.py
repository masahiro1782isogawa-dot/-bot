"""
習慣データを集計し、レポートデータを構築するモジュール。

設計方針:
  - Gemini は「名言 quote + 学び tips」を 1 リクエストで生成（無料枠の回数・バーストを抑える）。
  - 学びは habit_knowledge 参照。達成状況はプロンプトに含めない。
  - 失敗時は名言は静的リスト、学びは habit_knowledge から日付で決定論的に選択。
  - headline・greeting・score・streak のうち greeting はヘッダー用（スコア帯で変化）。

スコア計算ルール:
  - 1習慣 = 20点（5習慣 × 20点 = 100点満点）
  - 達成(done) → 20点 / スキップ(skip) → 0点
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# GitHub Actions は UTC 動作のため、JST (UTC+9) で日付を計算する
_JST = timezone(timedelta(hours=9))

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

RANK_THRESHOLDS = [
    (95, "S"),
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0, "D"),
]

POINTS_PER_HABIT = 20

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HABIT_KNOWLEDGE_DIR = _REPO_ROOT / "habit_knowledge"

_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n.*?\r?\n---\s*\r?\n", re.DOTALL)
_TIP_LINE_RE = re.compile(
    r"^\s*(?:[-*・●]|\d+[.、\)])\s*(.+?)\s*$",
)

# 知識ファイルが空・欠落のときの最小フォールバック（称賛・励まし文ではない）
_EMBEDDED_TIPS: dict[str, list[str]] = {
    "筋トレ": [
        "同一部位の直後48〜72時間は合成・炎症のバランスが重要で、分割と睡眠が回復の鍵になりやすい。",
        "漸進過負荷は「重量だけ」ではなくセット・回数・RIRのいずれかを週次で明文化するとブレにくい。",
    ],
    "ジャーナル": [
        "感情ラベリングは短い語で足りる。長文より「名前→身体感覚→次の一手」の順が実務で扱いやすい。",
        "書く目的を「正しさ」ではなく「観測」に置くと、欠損日が出てもデータとして復帰しやすい。",
    ],
    "瞑想": [
        "注意が逸れたら戻る回数が刺激。静坐は椅子でも可で、首肩代償を避けるほうが継続率に効くことが多い。",
        "延長呼気は副交感系へ働きかけやすい。数字固定より「吐くほうを長く」が運用しやすい。",
    ],
    "勉強": [
        "分散学習と検索練習は長期保持で再読より優位になりやすい、というエビデンスの整理が強い。",
        "インターリービングは当日の点数は下がることがあるが、区別課題で有利になりやすい。",
    ],
    "イメージング": [
        "プロセス想像（障害対処まで）は自己効力感の変化が出やすいという報告がある。",
        "運動イメージは実動作に近い皮質活動が示され、補助刺激として位置づけられる。",
    ],
}

_REPORT_AI_SYSTEM_INSTRUCTION = """\
あなたは習慣レポート用の JSON を 1 つだけ返す編集者です。

■ 出力形式（JSON のみ。マークダウン・コードブロック禁止）
{"quote":{"text":"50字以内の日本語の名言・格言","author":"著者名（日本語）"},"tip":{"habit":"習慣名","text":"80〜120字"}}

■ quote
- 習慣・自己成長・継続・実践に関連する、実在が確認できる日本語の名言または格言
- 英語は禁止

■ tip
- habit はユーザーが渡す 1 つの習慣名と完全一致
- text は 80〜120字、日本語のみ、箇条書き1行として使える文章にする
- 専門用語・難しい言葉は避け、中学生でもわかる言葉を使う
- すぐ実行できる具体的な行動を 1 つ入れ、前向きに短く締める
- ユーザーが渡す参考テキスト（habit_knowledge 抜粋）に沿う。要約・言い換え可。根拠のない捏造は禁止
- 日付はバリエーション用のヒントのみ。「記録」「達成」「スキップ」「点数」には触れない

■ 共通禁止
- 達成状況に基づく称賛・慰め・指示
"""

# 無料枠のトークン上限に寄りかからないよう、1習慣あたりの参考テキストは短めに切る
_MAX_KNOWLEDGE_CHARS_FOR_PROMPT = 2800

# 同一レポート内の API 回数を抑える: モデルは主に2種、各2試行まで
_GEMINI_MODEL_CANDIDATES = ["gemini-2.0-flash", "gemini-1.5-flash"]
_GEMINI_ATTEMPTS_PER_MODEL = 2


def _strip_frontmatter(text: str) -> str:
    text = text.lstrip("\ufeff")
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip("\r\n")


def _parse_tips_from_markdown(text: str) -> list[str]:
    """箇条書き・番号付き行を豆知識として抽出する。"""
    tips: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _TIP_LINE_RE.match(line)
        if m:
            body = m.group(1).strip()
            if body:
                tips.append(body)
        elif not line.startswith("|") and not line.startswith("```"):
            # 箇条書きでない短い行は本文が少ないファイル向けに拾う
            if len(line) <= 200 and "http" not in line.lower():
                tips.append(line)
    # 重複除去（順序維持）
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tips:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _read_text_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _load_habit_knowledge_sources(habit_name: str) -> str:
    """
    habit_knowledge/<習慣名>.md と habit_knowledge/<習慣名>/SKILL.md を結合する。
    SKILL.md の YAML フロントマターは除去する。
    """
    base = _HABIT_KNOWLEDGE_DIR
    chunks: list[str] = []
    md_path = base / f"{habit_name}.md"
    skill_path = base / habit_name / "SKILL.md"

    md_raw = _read_text_file(md_path)
    if md_raw:
        chunks.append(_strip_frontmatter(md_raw))

    skill_raw = _read_text_file(skill_path)
    if skill_raw:
        chunks.append(_strip_frontmatter(skill_raw))

    return "\n\n".join(c for c in chunks if c.strip())


def _tips_for_habit(habit_name: str) -> list[str]:
    combined = _load_habit_knowledge_sources(habit_name)
    tips = _parse_tips_from_markdown(combined)
    if tips:
        return tips
    return list(_EMBEDDED_TIPS.get(habit_name, []))


def _pick_tip_index(habit_name: str, target_date: date, n: int) -> int:
    if n <= 0:
        return 0
    seed = target_date.toordinal() * 1315423911 + sum(ord(c) * (i + 1) for i, c in enumerate(habit_name))
    return seed % n


def _pro_tip_for_habit(habit_name: str, target_date: date) -> str:
    tips = _tips_for_habit(habit_name)
    if not tips:
        return "この習慣の豆知識: habit_knowledge に .md または SKILL.md を追加してください。"
    return tips[_pick_tip_index(habit_name, target_date, len(tips))]


def _build_feedback_point_from_knowledge(
    habit: dict,
    target_date: date,
) -> list[str]:
    """選択された1習慣のみを1行で返す（Gemini失敗時フォールバック）。"""
    name = habit.get("name", "")
    emoji = habit.get("emoji", "")
    tip = _pro_tip_for_habit(name, target_date)
    prefix = f"{emoji} {name}".strip()
    return [f"{prefix} — {tip}" if prefix else tip]


def _line_from_tip_payload_strict(habit: dict, tip_payload: dict) -> list[str] | None:
    """
    Gemini の tip を行に整形。選択した習慣名と非空 text があるときだけ成功。
    """
    if not isinstance(tip_payload, dict):
        return None
    expected_name = str(habit.get("name", "")).strip()
    actual_name = str(tip_payload.get("habit", "")).strip()
    text = str(tip_payload.get("text", "")).strip()
    if not expected_name or expected_name != actual_name or not text:
        return None
    emoji = habit.get("emoji", "")
    prefix = f"{emoji} {expected_name}".strip()
    return [f"{prefix} — {text}" if prefix else text]


def _build_knowledge_bundle_for_prompt(habit_name: str) -> str:
    raw = _load_habit_knowledge_sources(habit_name)
    if raw.strip():
        if len(raw) > _MAX_KNOWLEDGE_CHARS_FOR_PROMPT:
            return raw[:_MAX_KNOWLEDGE_CHARS_FOR_PROMPT] + "\n\n（以下略）"
        return raw
    embedded = _EMBEDDED_TIPS.get(habit_name, [])
    if embedded:
        return "\n".join(f"- {t}" for t in embedded)
    return "（参考テキストなし。断定を避け、短い中立の学び1行に留める）"


# ──────────────────────────────────────────────
# フォールバック用の日本語名言リスト（31件）
# Gemini API が全モデルで失敗した場合に day-of-month で選択する
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
    return datetime.now(_JST).date()


# ──────────────────────────────────────────────
# スコア計算（5習慣 × 20点 = 100点満点）
# ──────────────────────────────────────────────

def _calc_score(habits: list[dict]) -> int:
    done_count = sum(1 for h in habits if h["status"] == "done")
    return done_count * POINTS_PER_HABIT


def _calc_streak(recent_days: list[dict]) -> int:
    """
    昨日から1日ずつ遡り、Notionに記録があり done_ratio >= 0.5 の日数をカウントする。
    記録が存在しない日はストリーク終了とみなす。
    """
    date_map: dict[date, float] = {e["date"]: e["done_ratio"] for e in recent_days}
    streak = 0
    yesterday = _today_jst() - timedelta(days=1)

    for i in range(len(recent_days) + 1):
        check_date = yesterday - timedelta(days=i)
        ratio = date_map.get(check_date)
        if ratio is None:
            break
        if ratio >= 0.5:
            streak += 1
        else:
            break

    return streak


def _calc_weekly(recent_days: list[dict]) -> tuple[int, str]:
    """
    今週（月曜〜昨日）の週間スコアとランクを返す。
    基準: yesterday を含む週の月曜日。_build_week_dots と同じ基準日を使う。
    """
    yesterday = _today_jst() - timedelta(days=1)
    # yesterday が属する週の月曜日（today ではなく yesterday 基準で統一）
    this_monday = yesterday - timedelta(days=yesterday.weekday())

    this_week = [e for e in recent_days if this_monday <= e["date"] <= yesterday]

    if not this_week:
        return 0, "D"

    avg = sum(e["done_ratio"] for e in this_week) / len(this_week) * 100
    weekly_score = round(avg)
    rank = next(r for threshold, r in RANK_THRESHOLDS if weekly_score >= threshold)
    return weekly_score, rank


def _build_week_dots(recent_days: list[dict]) -> list[dict]:
    """
    週ドットを生成する。
    基準: yesterday を含む週の月曜日（_calc_weekly と同じ基準日）。
    today 基準にすると月曜日の朝に週全体が「未来」ドットになるバグを修正。
    """
    yesterday = _today_jst() - timedelta(days=1)
    # yesterday が属する週の月曜日（today ではなく yesterday 基準で統一）
    monday = yesterday - timedelta(days=yesterday.weekday())
    recent_map = {e["date"]: e["done_ratio"] for e in recent_days}

    dots = []
    for i in range(7):
        d = monday + timedelta(days=i)
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
        dots.append({"label": WEEKDAY_JA[i], "css": css})

    return dots


def _format_date_ja(d: date) -> str:
    return f"{d.month}月{d.day}日({WEEKDAY_JA[d.weekday()]})"


# ──────────────────────────────────────────────
# 決定論的に生成する箇所（AI不要）
# ──────────────────────────────────────────────

def _build_headline(done_count: int, total: int, score: int) -> str:
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
    if score == 100:
        return "完璧な1日でした！素晴らしい！"
    if score >= 80:
        return "昨日の努力、お疲れ様でした！"
    if score >= 60:
        return "着実に前進しています。今日も続けましょう！"
    return "完璧でなくて大丈夫。今日また一歩踏み出しましょう！"


# ──────────────────────────────────────────────
# AI（Gemini）: 名言 + 日替わり学びを 1 リクエストで生成
# ──────────────────────────────────────────────


def _generate_quote_and_daily_tips(
    habits: list[dict],
    target_date: date,
) -> tuple[dict, list[str]]:
    """
    quote と tips を 1 回の generate_content で取得する（日次 RPD を抑える）。
    達成状況はプロンプトに含めない。失敗時は名言は静的リスト、学びは habit_knowledge の日付選択。
    """
    valid_habits = [h for h in habits if h.get("name")]
    if not valid_habits:
        fb_quote = _FALLBACK_QUOTES[target_date.toordinal() % len(_FALLBACK_QUOTES)]
        return {"quote": fb_quote}, ["今日の学びを生成できませんでした。習慣名を確認してください。"]
    selected_habit = random.choice(valid_habits)
    selected_name = selected_habit["name"]
    reference = _build_knowledge_bundle_for_prompt(selected_name)

    user_message = (
        f"日付（名言・学びのバリエーション用。達成記録やスコアとは無関係）: {target_date.isoformat()}\n\n"
        f"今回は次の1項目のみを対象に、学びを1件だけ作ってください: {selected_name}\n\n"
        "以下は対象習慣の参考テキスト（habit_knowledge の抜粋）です。"
        "tip はこの内容に基づき 1 件。quote は日本語の名言を 1 つ（参考テキストに合わせなくてよい）。\n\n"
        f"## {selected_name}\n{reference}"
    )

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    max_attempt_idx = _GEMINI_ATTEMPTS_PER_MODEL - 1

    for model in _GEMINI_MODEL_CANDIDATES:
        for attempt in range(_GEMINI_ATTEMPTS_PER_MODEL):
            try:
                logger.info(
                    "Gemini API（名言+学び 統合）モデル: %s, 試行: %s/%s",
                    model,
                    attempt + 1,
                    _GEMINI_ATTEMPTS_PER_MODEL,
                )
                response = client.models.generate_content(
                    model=model,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=_REPORT_AI_SYSTEM_INSTRUCTION,
                        temperature=0.55,
                        max_output_tokens=2000,
                        response_mime_type="application/json",
                    ),
                )
                result = json.loads(response.text)
                quote = result.get("quote")
                tip_raw = result.get("tip")
                if not (
                    isinstance(quote, dict)
                    and quote.get("text")
                    and quote.get("author")
                ):
                    logger.info(
                        "Gemini API: quote 不正（keys=%s）。リトライ/次モデルへ",
                        list(result.keys()),
                    )
                    break
                if not isinstance(tip_raw, dict):
                    logger.info(
                        "Gemini API: tip がオブジェクトではない（%s）。リトライ/次モデルへ",
                        type(tip_raw).__name__,
                    )
                    break
                lines = _line_from_tip_payload_strict(selected_habit, tip_raw)
                if lines is None:
                    logger.info(
                        "Gemini API: tip の habit 名または text が不正。リトライ/次モデルへ"
                    )
                    break
                logger.info("Gemini API 成功（名言+学び, モデル: %s）", model)
                return {"quote": {"text": quote["text"], "author": quote["author"]}}, lines
            except Exception as e:
                if attempt < max_attempt_idx:
                    wait = 10 * (2**attempt)
                    logger.warning(
                        "Gemini API エラー（名言+学び, %s, %s回目）: %s。%s秒後リトライ...",
                        model,
                        attempt + 1,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "Gemini API 失敗（名言+学び, %s 全試行終了）: %s", model, e
                    )

    logger.warning(
        "Gemini API が名言+学びで全モデル失敗。"
        "名言は静的リスト、学びは選択した1習慣の habit_knowledge から日付で選択します。"
    )
    fb_quote = _FALLBACK_QUOTES[target_date.toordinal() % len(_FALLBACK_QUOTES)]
    fb_lines = _build_feedback_point_from_knowledge(selected_habit, target_date)
    return {"quote": fb_quote}, fb_lines


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

    greeting = _build_greeting(score)

    ai_quote, knowledge_points = _generate_quote_and_daily_tips(habits, target_date)

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
                "headline": "今日の学び（5項目からランダムで1件）",
                "points": knowledge_points,
                "action": "",
            },
            "quote": ai_quote.get("quote", _FALLBACK_QUOTES[0]),
        },
        "done_count": done_count,
        "week_dots": week_dots,
    }
