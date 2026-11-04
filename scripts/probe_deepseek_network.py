"""Unauthenticated DeepSeek HTTPS preflight using the current Python/SDK proxy environment.

This probe never outputs or sends the API key and does not change proxy settings.
An HTTP 401 is a successful transport check: the request reached DeepSeek without
credentials. The OpenAI SDK's default httpx client uses this same trust_env path.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from urllib.request import proxy_bypass
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.m0.config import load_settings


PROXY_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _safe_proxy(value: str) -> str:
    if not value:
        return "UNSET"
    if value.lower() in {"*"}:
        return "SET(*)"
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        hostname = parsed.hostname
        port_number = parsed.port
    except ValueError:
        return "SET(redacted; unrecognized format)"
    if not hostname:
        return "SET(redacted; unrecognized format)"
    port = f":{port_number}" if port_number else ""
    return f"SET({parsed.scheme + '://' if parsed.scheme else ''}{hostname}{port})"


def _proxy_socket(value: str) -> str:
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if not parsed.hostname or parsed.port is None:
            return "NOT_CHECKED"
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return "REACHABLE"
    except OSError as exc:
        return f"UNREACHABLE({type(exc).__name__})"


def main() -> int:
    settings = load_settings(require_api_key=False)
    for name in PROXY_NAMES:
        print(f"{name}={_safe_proxy(os.environ.get(name, ''))}")

    print(f"HTTPX_VERSION={httpx.__version__}")
    print("HTTPX_TRUST_ENV=True (OpenAI SDK default)")
    checked: set[str] = set()
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name, "")
        if value and value not in checked:
            checked.add(value)
            print(f"PROXY_SOCKET_{_safe_proxy(value)}={_proxy_socket(value)}")

    endpoint = settings.base_url.rstrip("/") + "/"
    parsed_endpoint = urlsplit(endpoint)
    print(f"DEEPSEEK_HOST={parsed_endpoint.hostname or 'INVALID'}")
    try:
        print(f"NO_PROXY_BYPASSES_DEEPSEEK={proxy_bypass(parsed_endpoint.hostname or '')}")
    except (OSError, ValueError):
        print("NO_PROXY_BYPASSES_DEEPSEEK=UNKNOWN")
    print(f"MODEL_ID={settings.model}")
    print("API_KEY_CONFIGURED=" + ("yes" if bool(settings.api_key.strip()) else "no"))
    print("TLS_VERIFY=True")
    try:
        # This is intentionally the default environment-aware HTTPX path used by
        # OpenAI(api_key=..., base_url=...) when no custom http_client is supplied.
        with httpx.Client(timeout=8.0, trust_env=True) as client:
            response = client.get(endpoint)
        if response.status_code == 401:
            print("NETWORK_STATUS=REACHABLE_UNAUTHENTICATED (HTTP 401)")
            return 0
        if 200 <= response.status_code < 300:
            print(f"NETWORK_STATUS=REACHABLE (HTTP {response.status_code})")
            return 0
        print(f"NETWORK_STATUS=HTTP_RESPONSE (HTTP {response.status_code}; auth/endpoint policy may apply)")
        return 2
    except httpx.ProxyError as exc:
        print(f"NETWORK_STATUS=PROXY_ERROR ({type(exc).__name__})")
        return 3
    except httpx.ConnectError as exc:
        print(f"NETWORK_STATUS=CONNECT_ERROR ({type(exc).__name__})")
        return 3
    except httpx.TimeoutException as exc:
        print(f"NETWORK_STATUS=TIMEOUT ({type(exc).__name__})")
        return 3
    except httpx.HTTPError as exc:
        print(f"NETWORK_STATUS=HTTP_CLIENT_ERROR ({type(exc).__name__})")
        return 3
    except ValueError as exc:
        print(f"NETWORK_STATUS=CONFIG_ERROR ({type(exc).__name__})")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
