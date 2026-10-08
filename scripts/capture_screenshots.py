"""Capture the README screenshots at 1280px wide from two frozen-clock dashboards.

    scripts/screenshots.sh        # does everything below and stops the servers afterwards

Needs `pip install playwright` and Chromium (or CHROME=/path/to/chrome).
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", default="http://127.0.0.1:8766", help="dashboard frozen mid-morning")
    ap.add_argument("--end", default="http://127.0.0.1:8767", help="dashboard frozen after the deadline")
    ap.add_argument("--branch", default="BR20")
    ap.add_argument("--copy-to", default="")
    ap.add_argument("--also", default="", help="also write copies with this suffix to /tmp (for review)")
    ap.add_argument("--width", type=int, default=1280)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    chrome = os.getenv("CHROME")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
        page = browser.new_page(viewport={"width": a.width, "height": 900}, device_scale_factor=1,
                                color_scheme="dark")

        def shot(url: str, name: str, height: int | None = None) -> Path:
            page.set_viewport_size({"width": a.width, "height": 900})
            page.goto(url, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            target = OUT / f"{name}.png"
            if height:
                h = min(height, page.evaluate("document.documentElement.scrollHeight"))
                page.set_viewport_size({"width": a.width, "height": h})
                page.wait_for_timeout(150)
                page.screenshot(path=str(target))
            else:
                page.screenshot(path=str(target), full_page=True)
            print(target)
            return target

        files = [
            shot(a.mid + "/", "1-board", 1000),
            shot(a.mid + "/?f=human", "2-needs-human", 1000),
            shot(a.mid + f"/b/{a.branch}", "3-branch-detail", 1400),
            shot(a.end + "/runs", "4-summary", 1500),
        ]
        browser.close()
    if a.copy_to:
        dest = Path(a.copy_to)
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dest / f.name)
    if a.also:
        for f in files:
            shutil.copy2(f, Path("/tmp") / f"{f.stem}-{a.also}.png")


if __name__ == "__main__":
    main()
