"""
Jinja2 でHTMLをレンダリングし、Playwright でPNG画像を生成するモジュール。
"""

import os
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
CARD_WIDTH = 390


def render_html(report_data: dict) -> str:
    """
    templates/report.html に report_data を埋め込んだ HTML 文字列を返す。

    Args:
        report_data: generate_report.build_report_data() の戻り値
    Returns:
        レンダリング済み HTML 文字列
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html")
    return template.render(**report_data)


def html_to_png(html: str, output_path: str | None = None) -> str:
    """
    HTML 文字列をスクリーンショットして PNG ファイルに保存する。

    Args:
        html: レンダリング済み HTML 文字列
        output_path: 保存先パス。None の場合は一時ファイルを生成する。
    Returns:
        保存した PNG ファイルの絶対パス
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".png", prefix="habit_report_")
        os.close(fd)

    with tempfile.NamedTemporaryFile(
        suffix=".html", mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(html)
        tmp_html = f.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = browser.new_page(
                viewport={"width": CARD_WIDTH + 48, "height": 900},
            )
            page.goto(f"file://{tmp_html}", wait_until="networkidle")

            # カード要素のみをクリップしてスクリーンショット
            card = page.locator(".card-root")
            card.screenshot(path=output_path)
            browser.close()
    finally:
        os.unlink(tmp_html)

    return output_path


def render_report_image(report_data: dict, output_path: str | None = None) -> str:
    """
    report_data → HTML レンダリング → PNG 生成の一連の処理を実行する。

    Returns:
        生成された PNG ファイルの絶対パス
    """
    html = render_html(report_data)
    return html_to_png(html, output_path)
