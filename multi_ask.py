#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_CHATGPT_HOST = "127.0.0.1"
DEFAULT_CHATGPT_PORT = 53165
DEFAULT_GEMINI_HOST = "127.0.0.1"
DEFAULT_GEMINI_PORT = 53168
SERVICE_ID = "multi-ask-daemon"
ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    host: str
    port: int
    script_path: Path
    expected_provider: str
    stdout_log: Path
    stderr_log: Path
    start_command: str


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    provider: Provider,
    timeout: float | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://{provider.host}:{provider.port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
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


def identity_mismatch(provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "identity_mismatch",
        "message": (
            f"{provider.host}:{provider.port} is occupied by an unexpected service "
            f"(service={payload.get('service')!r}, provider={payload.get('provider')!r}); expected "
            f"service={SERVICE_ID!r}, provider={provider.expected_provider!r}."
        ),
        "host": provider.host,
        "port": provider.port,
        "expected_service": SERVICE_ID,
        "expected_provider": provider.expected_provider,
        "actual_service": payload.get("service"),
        "actual_provider": payload.get("provider"),
    }


def validate_identity(provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("ok"):
        return payload
    if payload.get("service") == SERVICE_ID and payload.get("provider") == provider.expected_provider:
        return payload
    return identity_mismatch(provider, payload)


def probe_provider(provider: Provider) -> dict[str, Any]:
    return validate_identity(provider, request_json("GET", "/status", None, provider, timeout=2.0))


def start_provider(provider: Provider) -> None:
    command = [
        sys.executable,
        str(provider.script_path),
        "serve",
        "--host",
        provider.host,
        "--port",
        str(provider.port),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with provider.stdout_log.open("ab") as stdout, provider.stderr_log.open("ab") as stderr:
        subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=creationflags,
        )


def ensure_provider(provider: Provider, timeout_seconds: float = 30.0) -> dict[str, Any]:
    first = probe_provider(provider)
    if first.get("ok"):
        first.setdefault("autostarted", False)
        return first
    if first.get("code") != "daemon_unavailable":
        return first

    start_provider(provider)
    deadline = time.monotonic() + timeout_seconds
    last = first
    while time.monotonic() < deadline:
        time.sleep(0.5)
        last = probe_provider(provider)
        if last.get("ok"):
            last["autostarted"] = True
            return last
        if last.get("code") == "identity_mismatch":
            return last

    return {
        "ok": False,
        "code": "daemon_start_timeout",
        "message": f"Timed out waiting for {provider.label} daemon identity on {provider.host}:{provider.port}.",
        "start_command": provider.start_command,
        "stdout_log": str(provider.stdout_log),
        "stderr_log": str(provider.stderr_log),
        "last_error": last,
    }


async def call_provider(
    provider: Provider,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    ensure: bool,
) -> tuple[Provider, dict[str, Any]]:
    status = await asyncio.to_thread(ensure_provider if ensure else probe_provider, provider)
    if not status.get("ok"):
        return provider, status
    if method == "GET" and path == "/status":
        return provider, status
    result = await asyncio.to_thread(request_json, method, path, payload, provider, None)
    return provider, result


async def call_all(
    providers: list[Provider],
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    ensure: bool,
) -> dict[str, dict[str, Any]]:
    pairs = await asyncio.gather(*(call_provider(provider, method, path, payload, ensure) for provider in providers))
    return {provider.key: normalize_payload(provider, payload) for provider, payload in pairs}


def normalize_payload(provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "ok": bool(payload.get("ok")),
        "response": payload.get("response"),
        "error": None if payload.get("ok") else error_message(payload),
        "current_url": payload.get("current_url"),
        "request_id": payload.get("request_id"),
    }
    for key, value in payload.items():
        normalized.setdefault(key, value)
    normalized.setdefault("provider", provider.expected_provider)
    normalized["provider_label"] = provider.label
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
            script_path=ROOT / "chatgpt_agent.py",
            expected_provider="chatgpt",
            stdout_log=ROOT / "chatgpt_agent.out.log",
            stderr_log=ROOT / "chatgpt_agent.err.log",
            start_command=f"{sys.executable} chatgpt_agent.py serve --host {args.chatgpt_host} --port {args.chatgpt_port}",
        ),
        Provider(
            key="gemini",
            label="Gemini",
            host=args.gemini_host,
            port=args.gemini_port,
            script_path=ROOT / "gemini_agent.py",
            expected_provider="gemini",
            stdout_log=ROOT / "gemini_agent.out.log",
            stderr_log=ROOT / "gemini_agent.err.log",
            start_command=f"{sys.executable} gemini_agent.py serve --host {args.gemini_host} --port {args.gemini_port}",
        ),
    ]


def print_json(results: dict[str, dict[str, Any]]) -> None:
    print(json.dumps(results, ensure_ascii=False, indent=2))


def print_markdown(results: dict[str, dict[str, Any]]) -> None:
    for payload in results.values():
        label = payload["provider_label"]
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
    parser.add_argument(
        "--provider",
        choices=["both", "chatgpt", "gemini"],
        default="both",
        help="Which provider to use. Default: both.",
    )
    parser.add_argument("--chatgpt-host", default=DEFAULT_CHATGPT_HOST)
    parser.add_argument("--chatgpt-port", type=int, default=DEFAULT_CHATGPT_PORT)
    parser.add_argument("--gemini-host", default=DEFAULT_GEMINI_HOST)
    parser.add_argument("--gemini-port", type=int, default=DEFAULT_GEMINI_PORT)
    parser.add_argument("--json", action="store_true")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send prompts to ChatGPT and Gemini daemons in parallel.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="Ask one or both providers and print raw replies.")
    add_common_provider_args(ask_parser)
    ask_parser.add_argument("prompt", nargs="?", help="Prompt text. Omit when using --prompt-file.")
    ask_parser.add_argument("--prompt-file", help="Read prompt text from this UTF-8 file.")
    ask_parser.add_argument("--timeout", type=float, default=180.0)
    ask_parser.add_argument("--stable-seconds", type=float, default=5.0)

    status_parser = subparsers.add_parser("status", help="Get both daemon statuses.")
    add_common_provider_args(status_parser)
    status_parser.add_argument("--ensure", action="store_true", help="Start missing daemons before reporting status.")

    new_chat_parser = subparsers.add_parser("new-chat", help="Start fresh conversations in both providers.")
    add_common_provider_args(new_chat_parser)

    return parser.parse_args(argv)


def selected_providers(args: argparse.Namespace) -> list[Provider]:
    providers = provider_list(args)
    if args.provider == "both":
        return providers
    return [provider for provider in providers if provider.key == args.provider]


def load_prompt(args: argparse.Namespace) -> None:
    if args.command != "ask":
        return
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as file:
            prompt = file.read()
    if not prompt:
        print("Missing prompt or --prompt-file.", file=sys.stderr)
        raise SystemExit(2)
    args.prompt = prompt


async def run(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    providers = selected_providers(args)
    if args.command == "ask":
        return await call_all(
            providers,
            "POST",
            "/ask",
            {"prompt": args.prompt, "timeout": args.timeout, "stable_seconds": args.stable_seconds},
            ensure=True,
        )
    if args.command == "status":
        return await call_all(providers, "GET", "/status", None, ensure=args.ensure)
    if args.command == "new-chat":
        return await call_all(providers, "POST", "/new-chat", {}, ensure=True)
    raise RuntimeError(f"Unhandled command: {args.command}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    load_prompt(args)
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
