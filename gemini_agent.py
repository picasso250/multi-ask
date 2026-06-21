#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import Browser, Page, async_playwright


DEFAULT_CDP = "http://127.0.0.1:9222"
DEFAULT_GEMINI_URL = "https://gemini.google.com/app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 53168
SERVICE_ID = "multi-ask-daemon"
PROVIDER_ID = "gemini"
INPUT_SELECTOR = 'div.ql-editor[contenteditable="true"], div[role="textbox"][aria-label*="Gemini"]'
SEND_SELECTOR = 'button[aria-label*="Send"], button[aria-label*="Submit"], button.send-button'


class AgentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Job:
    request_id: str
    prompt: str
    timeout: float
    stable_seconds: float
    future: asyncio.Future[dict[str, Any]]
    queued_at: float = field(default_factory=time.time)


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
        raise AgentError("element_not_visible", f"Element has no visible bounding box: {selector}")
    await pre_mouse_wait()
    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def browser_ws_endpoint(cdp_url: str) -> str:
    with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    endpoint = payload.get("webSocketDebuggerUrl")
    if not endpoint:
        raise AgentError("cdp_missing_ws", "Chrome DevTools did not return webSocketDebuggerUrl.")
    return endpoint.replace("ws://localhost:", "ws://127.0.0.1:")


async def response_texts(page: Page) -> list[str]:
    return await page.evaluate(
        """() => [...document.querySelectorAll('model-response')]
          .map((el) => (el.innerText || el.textContent || '').trim())
          .filter(Boolean)"""
    )


async def turn_count(page: Page) -> int:
    return await page.evaluate(
        """() => document.querySelectorAll('user-query, model-response').length"""
    )


async def composer_ready(page: Page) -> bool:
    return await page.evaluate(
        """() => {
          const box = document.querySelector('div.ql-editor[contenteditable="true"], div[role="textbox"][aria-label*="Gemini"]');
          if (!box) return false;
          const rect = box.getBoundingClientRect();
          const style = getComputedStyle(box);
          if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') return false;
          return ![...document.querySelectorAll('button, [role="button"]')].some((button) => {
            const label = [
              button.getAttribute('aria-label'),
              button.innerText,
              button.textContent
            ].filter(Boolean).join(' ').toLowerCase();
            return label.includes('stop') || label.includes('停止');
          });
        }"""
    )


async def wait_for_response(
    page: Page,
    before_turn_count: int,
    before_response_count: int,
    timeout_seconds: float,
    stable_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    stable_since: float | None = None

    while time.monotonic() < deadline:
        turns = await turn_count(page)
        responses = await response_texts(page)
        current = responses[-1] if len(responses) > before_response_count and turns > before_turn_count else ""
        ready = await composer_ready(page)

        if current and current == last_text and ready:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds:
                return current
        else:
            stable_since = None
            last_text = current

        await asyncio.sleep(0.5)

    raise AgentError("response_timeout", "Timed out waiting for a stable Gemini response.")


class GeminiAgent:
    def __init__(self, cdp_url: str, target_url: str) -> None:
        self.cdp_url = cdp_url
        self.target_url = target_url
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.playwright_manager = None
        self.action_lock = asyncio.Lock()
        self.running_request_id: str | None = None
        self.last_error: dict[str, Any] | None = None
        self.started_at = time.time()

    async def start(self) -> None:
        ws_endpoint = browser_ws_endpoint(self.cdp_url)
        self.playwright_manager = async_playwright()
        playwright = await self.playwright_manager.start()
        self.browser = await playwright.chromium.connect_over_cdp(ws_endpoint)
        self.page = await self.find_or_open_page()
        await self.page.bring_to_front()
        await stable_wait()
        asyncio.create_task(self.worker())

    async def stop(self) -> None:
        if self.browser:
            await self.browser.close()
        if self.playwright_manager:
            await self.playwright_manager.stop()

    async def find_or_open_page(self) -> Page:
        if not self.browser:
            raise AgentError("browser_not_ready", "Browser is not connected.")
        gemini_page: Page | None = None
        for context in self.browser.contexts:
            for page in context.pages:
                if page.is_closed():
                    continue
                if page.url == self.target_url:
                    return page
                if page.url.startswith("https://gemini.google.com/app"):
                    gemini_page = gemini_page or page
        if gemini_page:
            return gemini_page

        context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        page = await context.new_page()
        await page.goto(self.target_url)
        await stable_wait()
        return page

    async def ensure_page(self) -> Page:
        if self.page and not self.page.is_closed():
            return self.page
        self.page = await self.find_or_open_page()
        return self.page

    async def new_chat(self) -> dict[str, Any]:
        async with self.action_lock:
            self.running_request_id = "new-chat"
            try:
                if not self.browser:
                    raise AgentError("browser_not_ready", "Browser is not connected.")
                context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
                page = await context.new_page()
                await page.goto(self.target_url)
                await page.bring_to_front()
                await stable_wait()
                self.page = page
                return {"ok": True, "current_url": self.page.url}
            finally:
                self.running_request_id = None

    async def enqueue_ask(self, prompt: str, timeout: float, stable_seconds: float) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        job = Job(
            request_id=str(uuid.uuid4()),
            prompt=prompt,
            timeout=timeout,
            stable_seconds=stable_seconds,
            future=future,
        )
        await self.queue.put(job)
        return await future

    async def worker(self) -> None:
        while True:
            job = await self.queue.get()
            self.running_request_id = job.request_id
            try:
                result = await self.handle_ask(job)
                self.last_error = None
                job.future.set_result(result)
            except AgentError as exc:
                error = self.error_payload(exc.code, exc.message, job.request_id)
                self.last_error = error
                job.future.set_result(error)
            except Exception as exc:
                error = self.error_payload("unexpected_error", str(exc), job.request_id)
                self.last_error = error
                job.future.set_result(error)
            finally:
                self.running_request_id = None
                self.queue.task_done()

    async def handle_ask(self, job: Job) -> dict[str, Any]:
        async with self.action_lock:
            page = await self.ensure_page()
            await page.bring_to_front()
            await stable_wait()

            before_turns = await turn_count(page)
            before_responses = await response_texts(page)
            await click_element_center(page, INPUT_SELECTOR)
            await type_like_user(page, job.prompt)
            await click_element_center(page, SEND_SELECTOR)
            response = await wait_for_response(
                page,
                before_turns,
                len(before_responses),
                job.timeout,
                job.stable_seconds,
            )
            return {
                "ok": True,
                "request_id": job.request_id,
                "response": response,
                "current_url": page.url,
            }

    def status(self) -> dict[str, Any]:
        current_url = None if not self.page or self.page.is_closed() else self.page.url
        return {
            "ok": True,
            "service": SERVICE_ID,
            "provider": PROVIDER_ID,
            "busy": self.running_request_id is not None,
            "running_request_id": self.running_request_id,
            "queue_length": self.queue.qsize(),
            "current_url": current_url,
            "last_error": self.last_error,
            "uptime_seconds": round(time.time() - self.started_at, 3),
        }

    def error_payload(self, code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
        current_url = None if not self.page or self.page.is_closed() else self.page.url
        return {
            "ok": False,
            "code": code,
            "message": message,
            "request_id": request_id,
            "current_url": current_url,
        }


def json_response(payload: dict[str, Any], status: int = 200) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii") + body


async def read_http_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    header_bytes = await reader.readuntil(b"\r\n\r\n")
    header_text = header_bytes.decode("iso-8859-1")
    lines = header_text.split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return method, path, headers, body


async def handle_http_client(agent: GeminiAgent, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, path, _headers, body = await read_http_request(reader)
        if method == "GET" and path == "/status":
            payload = agent.status()
        elif method == "POST" and path == "/ask":
            data = json.loads(body.decode("utf-8") or "{}")
            prompt = str(data.get("prompt", ""))
            if not prompt:
                raise AgentError("bad_request", "Missing prompt.")
            timeout = float(data.get("timeout", 180.0))
            stable_seconds = float(data.get("stable_seconds", 5.0))
            payload = await agent.enqueue_ask(prompt, timeout, stable_seconds)
        elif method == "POST" and path == "/new-chat":
            payload = await agent.new_chat()
        else:
            writer.write(json_response({"ok": False, "code": "not_found", "message": "Unknown endpoint."}, 404))
            await writer.drain()
            return
        writer.write(json_response(payload, 200 if payload.get("ok") else 500))
    except AgentError as exc:
        writer.write(json_response({"ok": False, "code": exc.code, "message": exc.message}, 400))
    except Exception as exc:
        writer.write(json_response({"ok": False, "code": "server_error", "message": str(exc)}, 500))
    finally:
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def serve(args: argparse.Namespace) -> None:
    agent = GeminiAgent(args.cdp_url, args.url)
    server = await asyncio.start_server(
        lambda reader, writer: handle_http_client(agent, reader, writer),
        args.host,
        args.port,
    )
    try:
        await agent.start()
    except Exception:
        server.close()
        await server.wait_closed()
        await agent.stop()
        raise
    sockets = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"Gemini agent listening on {sockets}", flush=True)
    async with server:
        try:
            await server.serve_forever()
        finally:
            await agent.stop()


def request_json(method: str, path: str, payload: dict[str, Any] | None, host: str, port: int) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://{host}:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=None) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "code": "http_error", "message": raw or str(exc)}
    except URLError as exc:
        return {"ok": False, "code": "daemon_unavailable", "message": str(exc.reason)}


def print_payload(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload.get("ok") and "response" in payload:
        print(payload["response"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr if not payload.get("ok") else sys.stdout)
    return 0 if payload.get("ok") else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini browser automation daemon and CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the local daemon.")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--cdp-url", default=DEFAULT_CDP)
    serve_parser.add_argument("--url", default=DEFAULT_GEMINI_URL)

    ask_parser = subparsers.add_parser("ask", help="Send one prompt through the daemon.")
    ask_parser.add_argument("--host", default=DEFAULT_HOST)
    ask_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--timeout", type=float, default=180.0)
    ask_parser.add_argument("--stable-seconds", type=float, default=5.0)
    ask_parser.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status", help="Get daemon status.")
    status_parser.add_argument("--host", default=DEFAULT_HOST)
    status_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    status_parser.add_argument("--json", action="store_true")

    new_chat_parser = subparsers.add_parser("new-chat", help="Open a fresh Gemini tab.")
    new_chat_parser.add_argument("--host", default=DEFAULT_HOST)
    new_chat_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    new_chat_parser.add_argument("--json", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "serve":
        try:
            asyncio.run(serve(args))
        except KeyboardInterrupt:
            return 0
        return 0
    if args.command == "ask":
        payload = request_json(
            "POST",
            "/ask",
            {"prompt": args.prompt, "timeout": args.timeout, "stable_seconds": args.stable_seconds},
            args.host,
            args.port,
        )
        return print_payload(payload, args.json)
    if args.command == "status":
        return print_payload(request_json("GET", "/status", None, args.host, args.port), args.json)
    if args.command == "new-chat":
        return print_payload(request_json("POST", "/new-chat", {}, args.host, args.port), args.json)
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
