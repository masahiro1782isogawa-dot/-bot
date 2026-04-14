"""
習慣データを集計し、レポートデータを構築するモジュール。

設計方針:
  - AI（Gemini）は 1回の呼び出し で coaching（points + action）と quote を同時生成する。
    → 翻訳コールや ZenQuotes 依存をなくし、API呼び出し回数を最小化。
  - Gemini の system_instruction に専門コーチペルソナ＋知識を埋め込む。
    → プロンプトに知識ファイルを丸ごと注入する方式は廃止（Geminiが指示と資料を混同するため）。
  - headline・greeting・score・streak は決定論的に生成し、出力を安定させる。

スコア計算ルール:
  - 1習慣 = 20点（5習慣 × 20点 = 100点満点）
  - 達成(done) → 20点 / スキップ(skip) → 0点
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

from google import genai
from google.genai import types

# GitHub Actions は UTC 動作のため、JST (UTC+9) で日付を計算する
_JST = timezone(timedelta(hours=9))

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

RANK_THRESHOLDS = [
    (95, "S"),
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0,  "D"),
]

POINTS_PER_HABIT = 20

# ──────────────────────────────────────────────
# Gemini system_instruction
# 専門コーチペルソナ＋各習慣の核心知識＋出力規則を一箇所に集約する。
# ここを変えるだけでフィードバック品質が変わる。
# ──────────────────────────────────────────────
_COACHING_SYSTEM_INSTRUCTION = """\
あなたは習慣形成・健康・メンタルの専門コーチです。
ユーザーの昨日の習慣達成状況を見て、プロフェッショナルなフィードバックを生成してください。

■ 各習慣の専門知識（必ずこの知識を活かした具体的なコメントをしてください）

💪筋トレ
・超回復に48〜72時間必要。毎日同じ部位は逆効果
・プログレッシブオーバーロード（負荷の漸進）が筋成長の必須条件
・運動後30〜60分以内のタンパク質摂取が筋タンパク質合成を最大化
・気乗りしない日でも5分だけ動くことで継続の価値がある（最小有効量の原則）

📓ジャーナル
・感情を言語化するだけで扁桃体の興奮が抑制される（アフェクト・ラベリング）
・「うまくいったこと3つ」を書く手法が最もエビデンスが高い（ハーバード研究）
・5分・1文でも十分な心理的効果がある
・フリーライティングでワーキングメモリを解放できる

🧘瞑想
・8週間継続で前頭前皮質が肥厚する（MRI研究で確認）
・4-7-8呼吸法（吸4秒→止7秒→吐8秒）で副交感神経を即座に活性化
・ラベリング法（「考えている」とラベルを貼り呼吸に戻る）で雑念に対処
・特定の場所・時間に紐づけることで習慣化が3倍速くなる

📚勉強
・間隔反復（スペーシング効果）で長期記憶定着が2〜3倍向上
・テスト効果：読む・聞くより「思い出す」練習が記憶定着率40%高い
・就寝前学習が海馬→皮質への記憶転送を最も促進する
・25分集中→5分休憩のポモドーロ・テクニックが集中の最適解

🌟イメージング（ビジュアライゼーション）
・動作をイメージするだけで実際の動作と同じ神経回路が活性化（EMG研究確認）
・視覚だけでなく体感・感情・音を含めた多感覚イメージが最も効果的
・1人称視点でイメージすると神経活性化がより強い
・結果だけでなくプロセス（困難を乗り越える場面）もイメージすることが重要

■ フィードバック生成の規則

1. 達成した習慣への点（points[0]）
   → その習慣の生理学・心理学的価値を具体的に言語化する
   → 「よく頑張りました」等の一般的称賛は禁止
   → 例：「筋トレ後の超回復で今夜成長ホルモンが分泌されます」

2. スキップした習慣への点（points[1]）
   → 自責させず、超最小限の具体的な次アクションを提示する
   → 「頑張りましょう」等の常套句は禁止
   → 例：「明日は呼吸3回だけでも瞑想は成立します」

3. 今日のaction
   → 明日実行できる最も重要な具体的行動を1つだけ

4. quote（今日の名言）
   → 習慣・自己成長・継続に関連した日本語の実在する名言または格言
   → 英語は厳禁。必ず日本語で

■ 出力形式（JSONのみ。マークダウン・コードブロック不要）

{
  "points": [
    "達成習慣への専門的コメント（40字以内）",
    "スキップ習慣へのセルフコンパッション＋最小アクション（40字以内）"
  ],
  "action": "今日のaction ▶ 具体的な1アクション（50字以内）",
  "quote": {
    "text": "日本語の名言（50字以内）",
    "author": "著者名"
  }
}
"""

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
    """今週（月曜〜昨日）の週間スコアとランクを返す。"""
    yesterday = _today_jst() - timedelta(days=1)
    this_monday = yesterday - timedelta(days=yesterday.weekday())

    this_week = [e for e in recent_days if this_monday <= e["date"] <= yesterday]

    if not this_week:
        return 0, "D"

    avg = sum(e["done_ratio"] for e in this_week) / len(this_week) * 100
    weekly_score = round(avg)
    rank = next(r for threshold, r in RANK_THRESHOLDS if weekly_score >= threshold)
    return weekly_score, rank


def _build_week_dots(recent_days: list[dict]) -> list[dict]:
    yesterday = _today_jst() - timedelta(days=1)
    today = _today_jst()
    monday = today - timedelta(days=today.weekday())
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
# AI（Gemini）を使う箇所: coaching + quote を1回で生成
#
# 設計上の理由:
#   - 翻訳コール・ZenQuotes コールを廃止し API 呼び出しを1回に集約
#   - system_instruction で専門コーチペルソナを定義（プロンプトへの知識丸ごと注入より効果的）
#   - max_output_tokens=500 で出力の切り捨てを防止
#   - temperature=0.75 で具体性を保ちながらも毎日異なる内容を生成
# ──────────────────────────────────────────────

def _generate_ai_content(
    habits: list[dict],
    score: int,
    streak: int,
    target_date: date,
) -> dict:
    """
    Gemini で coaching（points + action）と quote を同時生成する。
    失敗時はプログラムで生成した静的フォールバックを返す。
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    done_names = [h["name"] for h in habits if h["status"] == "done"]
    skip_names = [h["name"] for h in habits if h["status"] == "skip"]
    done_count = len(done_names)
    total = len(habits)

    habit_summary = "\n".join(
        f"- {h['emoji']} {h['name']}: {'✓完了' if h['status'] == 'done' else 'スキップ'}"
        + (f" （{h['detail']}）" if h["detail"] and h["detail"] != "未実施" else "")
        for h in habits
    )

    user_message = (
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
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=_COACHING_SYSTEM_INSTRUCTION,
                        temperature=0.75,
                        max_output_tokens=500,
                        response_mime_type="application/json",
                    ),
                )
                result = json.loads(response.text)
                # 必須キーが揃っているか確認
                if (
                    "points" in result
                    and "action" in result
                    and "quote" in result
                    and isinstance(result["quote"], dict)
                    and result["quote"].get("text")
                    and result["quote"].get("author")
                ):
                    print(f"Gemini API 成功（モデル: {model}）")
                    return result
                print(f"Gemini API: 必須キー不足（{list(result.keys())}）。フォールバックへ")
                break
            except Exception as e:
                if attempt < 2:
                    wait = 10 * (2 ** attempt)
                    print(f"Gemini API エラー（{model}, {attempt + 1}回目）: {e}。{wait}秒後リトライ...")
                    time.sleep(wait)
                else:
                    print(f"Gemini API 失敗（{model} 全試行終了）: {e}")
                    break

    # 全モデル失敗時のフォールバック
    print("警告: Gemini API が全モデルで失敗しました。静的コンテンツで続行します。")
    skip_text = f"{skip_names[0]}は明日5分だけ試してみましょう。" if skip_names else "今日も習慣を続けましょう。"
    done_text = f"{done_names[0]}の継続が確実に身体・脳を変えています。" if done_names else "小さな一歩が積み重なっていきます。"
    fallback_quote = _FALLBACK_QUOTES[target_date.toordinal() % len(_FALLBACK_QUOTES)]
    return {
        "points": [done_text, skip_text],
        "action": f"今日のaction ▶ {skip_text if skip_names else '今日の習慣を1つ選んで実行する'}",
        "quote": fallback_quote,
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

    headline = _build_headline(done_count, len(habits), score)
    greeting = _build_greeting(score)

    # coaching と quote を1回の Gemini 呼び出しで生成
    ai_content = _generate_ai_content(habits, score, streak, target_date)

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
                "points": ai_content.get("points", []),
                "action": ai_content.get("action", ""),
            },
            "quote": ai_content.get("quote", _FALLBACK_QUOTES[0]),
        },
        "done_count": done_count,
        "week_dots": week_dots,
    }
