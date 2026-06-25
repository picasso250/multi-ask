#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from urllib.request import urlopen

from playwright.async_api import Page, async_playwright


DEFAULT_CDP = "http://127.0.0.1:9222"
DEFAULT_URL = "https://chatgpt.com/"


async def stable_wait() -> None:
    await asyncio.sleep(2.0)


async def pre_mouse_wait() -> None:
    await asyncio.sleep(0.2)


async def type_like_user(page: Page, text: str) -> None:
    for char in text:
        await asyncio.sleep(0.01)
        await page.keyboard.type(char)
        await asyncio.sleep(0.01)


async def click_element_center(page: Page, selector: str) -> None:
    locator = page.locator(selector).first
    await locator.wait_for(state="visible", timeout=15_000)
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError(f"Element has no visible bounding box: {selector}")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    await pre_mouse_wait()
    await page.mouse.click(x, y)


def browser_ws_endpoint(cdp_url: str) -> str:
    with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    endpoint = payload.get("webSocketDebuggerUrl")
    if not endpoint:
        raise RuntimeError("Chrome DevTools did not return webSocketDebuggerUrl.")
    return endpoint.replace("ws://localhost:", "ws://127.0.0.1:")


async def find_or_open_page(browser, target_url: str) -> Page:
    chatgpt_page: Page | None = None
    for context in browser.contexts:
        for page in context.pages:
            if page.url == target_url:
                return page
            if page.url.startswith("https://chatgpt.com/") and "/codex/" not in page.url:
                chatgpt_page = chatgpt_page or page

    if chatgpt_page:
        return chatgpt_page

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    await page.goto(target_url)
    return page


async def assistant_messages(page: Page) -> list[str]:
    return await page.evaluate(
        """() => [...document.querySelectorAll('[data-message-author-role="assistant"]')]
          .map((el) => (el.innerText || el.textContent || '').trim())
          .filter(Boolean)"""
    )


async def is_generating(page: Page) -> bool:
    return await page.evaluate(
        """() => {
          const buttons = [...document.querySelectorAll('button, [role="button"]')];
          return buttons.some((button) => {
            const label = [
              button.getAttribute('aria-label'),
              button.getAttribute('data-testid'),
              button.innerText,
              button.textContent
            ].filter(Boolean).join(' ').toLowerCase();
            return label.includes('stop') || label.includes('停止');
          });
        }"""
    )


async def wait_for_response(
    page: Page,
    before_count: int,
    timeout_seconds: float,
    stable_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    stable_since: float | None = None

    while time.monotonic() < deadline:
        messages = await assistant_messages(page)
        current = messages[-1] if len(messages) > before_count else ""

        if current and current == last_text:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds:
                return current
        else:
            stable_since = None
            last_text = current

        await asyncio.sleep(0.5)

    raise TimeoutError("Timed out waiting for a stable ChatGPT response.")


async def ask_chatgpt(args: argparse.Namespace) -> str:
    ws_endpoint = browser_ws_endpoint(args.cdp_url)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_endpoint)
        page = await find_or_open_page(browser, args.url)
        await page.bring_to_front()
        await stable_wait()

        before = await assistant_messages(page)
        await click_element_center(page, "#prompt-textarea")
        await type_like_user(page, args.prompt)

        send_selector = (
            'button[data-testid="send-button"], '
            'button[aria-label*="Send"], button[aria-label*="发送"]'
        )
        await click_element_center(page, send_selector)
        response = await wait_for_response(page, len(before), args.timeout, args.stable_seconds)

        await browser.close()
        return response


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one prompt to an open ChatGPT tab and print the response.")
    parser.add_argument("prompt", nargs="?", help="Prompt text. Omit when using --prompt-file.")
    parser.add_argument("--prompt-file", help="Read prompt text from this UTF-8 file.")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP, help=f"Chrome DevTools URL. Default: {DEFAULT_CDP}")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Exact ChatGPT tab URL to reuse. Default: {DEFAULT_URL}")
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds to wait for the response.")
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=5.0,
        help="Return after the last assistant message text is unchanged for this many seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as file:
            prompt = file.read()
    if not prompt:
        print("Missing prompt or --prompt-file.", file=sys.stderr)
        return 2
    args.prompt = prompt
    response = asyncio.run(ask_chatgpt(args))
    print(response)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
