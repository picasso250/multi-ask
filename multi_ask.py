#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_CHATGPT_HOST = "127.0.0.1"
DEFAULT_CHATGPT_PORT = 8765
DEFAULT_GEMINI_HOST = "127.0.0.1"
DEFAULT_GEMINI_PORT = 8768


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    host: str
    port: int
    start_command: str


def request_json(method: str, path: str, payload: dict[str, Any] | None, provider: Provider) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://{provider.host}:{provider.port}{path}",
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
        return {
            "ok": False,
            "code": "daemon_unavailable",
            "message": str(exc.reason),
            "start_command": provider.start_command,
        }
    except json.JSONDecodeError as exc:
        return {"ok": False, "code": "invalid_json", "message": str(exc)}


async def call_provider(
    provider: Provider,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> tuple[Provider, dict[str, Any]]:
    result = await asyncio.to_thread(request_json, method, path, payload, provider)
    return provider, result


async def call_all(
    providers: list[Provider],
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    pairs = await asyncio.gather(*(call_provider(provider, method, path, payload) for provider in providers))
    return {provider.key: normalize_payload(provider, payload) for provider, payload in pairs}


def normalize_payload(provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "ok": bool(payload.get("ok")),
        "response": payload.get("response"),
        "error": None if payload.get("ok") else error_message(payload),
        "current_url": payload.get("current_url"),
        "request_id": payload.get("request_id"),
    }
    if not payload.get("ok") and payload.get("start_command"):
        normalized["start_command"] = payload["start_command"]
    for key, value in payload.items():
        normalized.setdefault(key, value)
    normalized["provider"] = provider.label
    return normalized


def error_message(payload: dict[str, Any]) -> str:
    code = payload.get("code")
    message = payload.get("message")
    if code and message:
        return f"{code}: {message}"
    if message:
        return str(message)
    if code:
        return str(code)
    return json.dumps(payload, ensure_ascii=False)


def provider_list(args: argparse.Namespace) -> list[Provider]:
    return [
        Provider(
            key="chatgpt",
            label="ChatGPT",
            host=args.chatgpt_host,
            port=args.chatgpt_port,
            start_command=f"python chatgpt_agent.py serve --host {args.chatgpt_host} --port {args.chatgpt_port}",
        ),
        Provider(
            key="gemini",
            label="Gemini",
            host=args.gemini_host,
            port=args.gemini_port,
            start_command=f"python gemini_agent.py serve --host {args.gemini_host} --port {args.gemini_port}",
        ),
    ]


def print_json(results: dict[str, dict[str, Any]]) -> None:
    print(json.dumps(results, ensure_ascii=False, indent=2))


def print_markdown(results: dict[str, dict[str, Any]]) -> None:
    for key in ("chatgpt", "gemini"):
        payload = results[key]
        label = payload["provider"]
        print(f"## {label}")
        print()
        if payload.get("ok") and payload.get("response") is not None:
            print(payload["response"])
        elif payload.get("ok"):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {payload.get('error')}")
            if payload.get("start_command"):
                print()
                print(f"Start daemon: `{payload['start_command']}`")
        print()


def exit_code(results: dict[str, dict[str, Any]]) -> int:
    return 0 if all(payload.get("ok") for payload in results.values()) else 1


def add_common_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chatgpt-host", default=DEFAULT_CHATGPT_HOST)
    parser.add_argument("--chatgpt-port", type=int, default=DEFAULT_CHATGPT_PORT)
    parser.add_argument("--gemini-host", default=DEFAULT_GEMINI_HOST)
    parser.add_argument("--gemini-port", type=int, default=DEFAULT_GEMINI_PORT)
    parser.add_argument("--json", action="store_true")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send prompts to ChatGPT and Gemini daemons in parallel.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="Ask both providers and print both raw replies.")
    add_common_provider_args(ask_parser)
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--timeout", type=float, default=180.0)
    ask_parser.add_argument("--stable-seconds", type=float, default=5.0)

    status_parser = subparsers.add_parser("status", help="Get both daemon statuses.")
    add_common_provider_args(status_parser)

    new_chat_parser = subparsers.add_parser("new-chat", help="Start fresh conversations in both providers.")
    add_common_provider_args(new_chat_parser)

    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    providers = provider_list(args)
    if args.command == "ask":
        return await call_all(
            providers,
            "POST",
            "/ask",
            {"prompt": args.prompt, "timeout": args.timeout, "stable_seconds": args.stable_seconds},
        )
    if args.command == "status":
        return await call_all(providers, "GET", "/status", None)
    if args.command == "new-chat":
        return await call_all(providers, "POST", "/new-chat", {})
    raise RuntimeError(f"Unhandled command: {args.command}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    results = asyncio.run(run(args))
    if args.json:
        print_json(results)
    else:
        print_markdown(results)
    return exit_code(results)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
