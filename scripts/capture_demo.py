#!/usr/bin/env python3
"""Capture a responsive UI screenshot with an already-installed Playwright.

This script intentionally does not install browser binaries or generate a fake GIF.
Install `playwright` and its Chromium browser separately when you want captures.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def capture(url: str, output: Path, mobile: bool) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit("Install optional capture dependency: pip install playwright && playwright install chromium") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000},
            device_scale_factor=1,
        )
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.screenshot(path=str(output), full_page=True)
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5173")
    parser.add_argument("--output", type=Path, default=Path("docs/media/demo-arena.png"))
    parser.add_argument("--mobile", action="store_true")
    args = parser.parse_args()
    asyncio.run(capture(args.url, args.output, args.mobile))
    print(f"Screenshot saved to {args.output}")


if __name__ == "__main__":
    main()
