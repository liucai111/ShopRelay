"""Explicitly authorized, bounded HTTP transport for isolated DEV smoke runs.

This module is not wired into the ordinary runner. It never accepts a caller
supplied URL, follows redirects, retries a chat POST, or shares Synthetic grants.
Tests use only a loopback HTTPServer with synthetic responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.cookiejar
import json
import re
import ssl
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (HTTPCookieProcessor, HTTPRedirectHandler, Request,
                            HTTPSHandler, build_opener)


class DevAuthorizationError(PermissionError):
    pass


class DevTransportError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code)


_API_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class DevSmokeGrant:
    grant_id: str
    asset_id: str
    asset_version: str
    asset_sha256: str
    subject: str
    base_url: str
    environment: str
    expires_at_utc: str
    max_duration_seconds: int
    request_cap: int
    provider_call_cap: int
    embedding_call_cap: int
    token_cap: int
    usd_cap: float
    signature: str


class DevSmokeGrantAuthority:
    """Separate issuer for explicitly approved Dev assets and exact Dev hosts."""
    def __init__(self, *, issuer: str, trusted_issuers: set[str], signing_key: bytes,
                 allowed_targets: set[str], allowed_assets: Mapping[str, Mapping[str, str]]) -> None:
        if issuer not in trusted_issuers or len(signing_key) < 32:
            raise DevAuthorizationError("untrusted issuer or signing key too short")
        if not allowed_targets or not allowed_assets:
            raise DevAuthorizationError("Dev targets and assets must be explicitly allowlisted")
        self.issuer, self._key = issuer, bytes(signing_key)
        self.allowed_targets = frozenset(_canonical_base(item) for item in allowed_targets)
        self.allowed_assets = {key: dict(value) for key, value in allowed_assets.items()}
        if any(key.casefold().find("candidate") >= 0 or key.casefold().find("frozen") >= 0 or
               value.get("split") != "dev" for key, value in self.allowed_assets.items()):
            raise DevAuthorizationError("candidate, Frozen, or non-Dev asset cannot enter a Dev allowlist")
        self._revoked: set[str] = set()

    @staticmethod
    def _fields(grant: DevSmokeGrant) -> str:
        return "|".join((grant.grant_id, grant.asset_id, grant.asset_version,
            grant.asset_sha256, grant.subject, grant.base_url, grant.environment,
            grant.expires_at_utc, str(grant.max_duration_seconds), str(grant.request_cap), str(grant.provider_call_cap),
            str(grant.embedding_call_cap), str(grant.token_cap), str(grant.usd_cap)))

    def issue(self, *, asset_id: str, asset_version: str, asset_sha256: str,
              subject: str, base_url: str, ttl_minutes: int = 30,
              max_duration_seconds: int = 7200,
              request_cap: int = 8, provider_call_cap: int = 40,
              embedding_call_cap: int = 8, token_cap: int = 120_000,
              usd_cap: float = 5.0) -> DevSmokeGrant:
        base = _canonical_base(base_url)
        asset = self.allowed_assets.get(asset_id)
        if asset is None or base not in self.allowed_targets:
            raise DevAuthorizationError("asset or Dev target is not allowlisted")
        if asset_id.casefold().find("candidate") >= 0 or asset_id.casefold().find("frozen") >= 0:
            raise DevAuthorizationError("protected asset is never grantable")
        if (asset.get("split") != "dev" or asset.get("scoreability") != "ELIGIBLE" or
                asset.get("review_status") not in {"INDEPENDENT_REVIEWED", "BATCH_USER_CONFIRMED"} or
                asset.get("version") != asset_version or asset.get("sha256") != asset_sha256 or
                asset.get("exposure_status") != "UNEXPOSED_VERIFIED"):
            raise DevAuthorizationError("asset metadata is not eligible for a Dev grant")
        if not re.fullmatch(r"[0-9a-f]{64}", asset_sha256):
            raise DevAuthorizationError("asset digest must be a SHA-256 hex value")
        if not subject or not 1 <= ttl_minutes <= 120:
            raise DevAuthorizationError("subject or grant lifetime is invalid")
        if (max_duration_seconds != 7200 or request_cap != 8 or
                not 1 <= provider_call_cap <= 40 or not 1 <= embedding_call_cap <= 8):
            raise DevAuthorizationError("Dev request budget is outside the approved hard envelope")
        if token_cap != 120_000 or usd_cap != 5.0:
            raise DevAuthorizationError("token and cost ceilings must match the locked draft")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z")
        identity = hashlib.sha256(f"{asset_id}|{subject}|{asset_sha256}|{base}".encode()).hexdigest()[:20]
        unsigned = DevSmokeGrant(f"devgrant.{identity}", asset_id, asset_version,
            asset_sha256, subject, base, "ISOLATED_DEV", expires, max_duration_seconds, request_cap,
            provider_call_cap, embedding_call_cap, token_cap, usd_cap, "")
        signature = hmac.new(self._key, self._fields(unsigned).encode(), hashlib.sha256).hexdigest()
        return DevSmokeGrant(**{**unsigned.__dict__, "signature": signature})

    def revoke(self, grant: DevSmokeGrant) -> None:
        self._revoked.add(grant.grant_id)

    def verify(self, grant: DevSmokeGrant, *, subject: str, base_url: str) -> None:
        if type(grant) is not DevSmokeGrant or grant.grant_id in self._revoked:
            raise DevAuthorizationError("missing or revoked Dev grant")
        base = _canonical_base(base_url)
        expected = hmac.new(self._key, self._fields(grant).encode(), hashlib.sha256).hexdigest()
        try:
            expires = datetime.fromisoformat(grant.expires_at_utc.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise DevAuthorizationError("invalid grant expiry") from exc
        if (not hmac.compare_digest(expected, grant.signature) or grant.subject != subject or
                grant.base_url != base or base not in self.allowed_targets or
                grant.asset_id not in self.allowed_assets or grant.environment != "ISOLATED_DEV" or
                datetime.now(timezone.utc) >= expires):
            raise DevAuthorizationError("Dev grant scope, signature, or expiry invalid")


def _canonical_base(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or
            parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise DevAuthorizationError("base URL must be an origin without credentials or path")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise DevAuthorizationError("unencrypted HTTP is allowed only for loopback Mock services")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DevTransportError("REDIRECT_REJECTED", status=code)


class DevHttpTransport:
    """Cookie/CSRF client for the existing owner-scoped ShopRelay API."""
    synthetic_only = False
    MAX_BODY = 2 * 1024 * 1024

    def __init__(self, *, base_url: str, grant: DevSmokeGrant,
                 authority: DevSmokeGrantAuthority, subject: str,
                 username: str, password: str, timeout_seconds: float = 10.0,
                 stream_timeout_seconds: float = 300.0, max_requests: int = 128) -> None:
        authority.verify(grant, subject=subject, base_url=base_url)
        if not username or not password or timeout_seconds <= 0 or max_requests < 1:
            raise DevAuthorizationError("credentials and bounded transport limits are required")
        self.base_url = _canonical_base(base_url)
        self.grant, self.subject = grant, subject
        self._username, self._password = username, password
        if max_requests < 1 or max_requests > 256:
            raise DevAuthorizationError("HTTP request budget must be between 1 and 256")
        if stream_timeout_seconds <= 0 or stream_timeout_seconds > 300:
            raise DevAuthorizationError("SSE wait must be between 1 and 300 seconds")
        self._timeout, self._stream_timeout = timeout_seconds, stream_timeout_seconds
        self._max_requests, self._requests = max_requests, 0
        self._chat_posts = 0
        self._jar = http.cookiejar.CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar), _NoRedirect(),
                                    HTTPSHandler(context=ssl.create_default_context()))
        self._csrf: str | None = None
        self._authenticated = False
        self._seen_chat: set[tuple[str, str]] = set()

    def __repr__(self) -> str:
        return f"DevHttpTransport(base_url={self.base_url!r}, subject={self.subject!r}, credentials=<redacted>)"

    def _request(self, method: str, path: str, *, body: Mapping[str, Any] | None = None,
                 csrf: bool = False, accept: str = "application/json",
                 timeout: float | None = None) -> tuple[int, Mapping[str, str], bytes]:
        if self._requests >= self._max_requests:
            raise DevTransportError("HTTP_REQUEST_BUDGET_EXHAUSTED")
        if not path.startswith("/") or path.startswith("//"):
            raise DevTransportError("INVALID_API_PATH")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": accept, "User-Agent": "ShopRelay-M6.1-Dev-Eval/1"}
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["Origin"] = self.base_url
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            if not self._csrf:
                raise DevTransportError("CSRF_TOKEN_MISSING")
            headers["X-CSRF-Token"] = self._csrf
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        self._requests += 1
        try:
            with self._opener.open(request, timeout=timeout or self._timeout) as response:
                raw = response.read(self.MAX_BODY + 1)
                if len(raw) > self.MAX_BODY:
                    raise DevTransportError("RESPONSE_TOO_LARGE")
                return response.status, dict(response.headers.items()), raw
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise DevTransportError("REDIRECT_REJECTED", status=exc.code) from None
            raise DevTransportError("HTTP_ERROR", status=exc.code) from None
        except (TimeoutError, OSError, URLError) as exc:
            # Caller must treat POST failures as ambiguous and must not replay.
            if method == "POST":
                raise TimeoutError("CHAT_POST_OUTCOME_UNKNOWN") from None
            raise DevTransportError("HTTP_CONNECTION_FAILED") from None

    @staticmethod
    def _json(raw: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DevTransportError("INVALID_JSON_RESPONSE") from exc
        if not isinstance(value, Mapping):
            raise DevTransportError("INVALID_RESPONSE_SHAPE")
        return value

    def authenticate(self) -> str:
        if self._authenticated:
            return self.subject
        status, _, raw = self._request("GET", "/api/eval/dev/target")
        if status != 200:
            raise DevTransportError("ISOLATED_DEV_TARGET_UNAVAILABLE", status=status)
        target = self._json(raw)
        if target.get("environment") != "ISOLATED_DEV" or target.get("database") != "shoprelay_m11_test":
            raise DevAuthorizationError("remote API did not attest the isolated Dev database")
        status, _, raw = self._request("GET", "/api/demo/csrf")
        if status != 200:
            raise DevTransportError("CSRF_FETCH_FAILED", status=status)
        payload = self._json(raw)
        csrf = payload.get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            raise DevTransportError("CSRF_TOKEN_MISSING")
        self._csrf = csrf
        status, _, raw = self._request("POST", "/api/demo/login", body={
            "username": self._username, "password": self._password}, csrf=True)
        if status != 200:
            raise DevTransportError("LOGIN_FAILED", status=status)
        user = self._json(raw).get("user")
        if not isinstance(user, Mapping) or user.get("user_id") != self.subject:
            raise DevAuthorizationError("authenticated identity does not match grant subject")
        self._password = ""  # Drop the credential after the login response is verified.
        self._authenticated = True
        return self.subject

    def _require_auth(self) -> None:
        if not self._authenticated:
            raise DevAuthorizationError("authenticate before using the Dev API")

    def create_session(self, *, title: str) -> Mapping[str, Any]:
        self._require_auth()
        marker = title.removeprefix("m6.1-eval:")
        if not title.startswith("m6.1-eval:") or not re.fullmatch(r"[a-f0-9]{32}", marker):
            raise DevAuthorizationError("Dev sessions require a bounded evaluation marker title")
        prior = self.lookup_session(marker=marker)
        if prior:
            return {"session": dict(prior), "recovered": True}
        try:
            status, _, raw = self._request("POST", "/api/sessions", body={"title": title}, csrf=True)
        except TimeoutError:
            # A read-only exact marker lookup is safe; never retry the POST.
            recovered = self.lookup_session(marker=marker)
            if recovered:
                return {"session": dict(recovered), "recovered": True}
            raise TimeoutError("SESSION_CREATE_OUTCOME_UNKNOWN") from None
        if status != 200:
            raise DevTransportError("SESSION_CREATE_FAILED", status=status)
        return self._json(raw)

    def lookup_session(self, *, marker: str) -> Mapping[str, Any] | None:
        self._require_auth()
        if not re.fullmatch(r"[a-f0-9]{32}", marker):
            raise DevAuthorizationError("invalid evaluation session marker")
        try:
            status, _, raw = self._request("GET", f"/api/eval/dev/sessions/lookup?{urlencode({'marker': marker})}")
        except DevTransportError as exc:
            if exc.status == 404:
                return None
            raise
        if status != 200:
            raise DevTransportError("SESSION_LOOKUP_FAILED", status=status)
        session = self._json(raw).get("session")
        if not isinstance(session, Mapping) or session.get("title") != f"m6.1-eval:{marker}":
            raise DevTransportError("SESSION_LOOKUP_SCOPE_MISMATCH")
        return session

    def lookup_run(self, *, session_id: str, client_request_id: str) -> Mapping[str, Any] | None:
        self._require_auth()
        if not _API_ID.fullmatch(session_id) or not re.fullmatch(r"m61c:[a-f0-9]{32}:[A-Za-z0-9_.-]{1,64}", client_request_id):
            raise DevAuthorizationError("invalid owner-scoped Run lookup identifiers")
        query = urlencode({"session_id": session_id, "client_request_id": client_request_id})
        try:
            status, _, raw = self._request("GET", f"/api/eval/dev/runs/lookup?{query}")
        except DevTransportError as exc:
            if exc.status == 404:
                return None
            raise
        if status == 404:
            return None
        if status != 200:
            raise DevTransportError("RUN_LOOKUP_FAILED", status=status)
        run = self._json(raw).get("run")
        if not isinstance(run, Mapping) or run.get("session_id") != session_id:
            raise DevTransportError("RUN_LOOKUP_SCOPE_MISMATCH")
        return run

    def post_chat_stream(self, *, session_id: str, message: str,
                         client_request_id: str) -> Mapping[str, Any]:
        self._require_auth()
        if (not _API_ID.fullmatch(session_id) or not message or len(message) > 10000 or
                not re.fullmatch(r"m61c:[a-f0-9]{32}:[A-Za-z0-9_.-]{1,64}", client_request_id)):
            raise DevAuthorizationError("invalid bounded Dev Chat request")
        identity = (session_id, client_request_id)
        if identity not in self._seen_chat and self._chat_posts >= self.grant.request_cap:
            raise DevTransportError("DEV_CHAT_REQUEST_BUDGET_EXHAUSTED")
        if identity in self._seen_chat:
            prior = self.lookup_run(session_id=session_id, client_request_id=client_request_id)
            if prior:
                return {"run_id": prior.get("run_id"), "status": prior.get("status"), "recovered": True}
            raise TimeoutError("CHAT_POST_OUTCOME_UNKNOWN")
        self._seen_chat.add(identity)
        # Lookup-before-POST makes restart/replay recovery read-only when a Run exists.
        prior = self.lookup_run(session_id=session_id, client_request_id=client_request_id)
        if prior:
            return {"run_id": prior.get("run_id"), "status": prior.get("status"), "recovered": True}
        self._require_auth()
        self._chat_posts += 1
        try:
            status, headers, raw = self._request("POST", "/api/chat/stream", body={
                "session_id": session_id, "message": message,
                "client_request_id": client_request_id}, csrf=True, accept="text/event-stream",
                timeout=self._stream_timeout)
        except TimeoutError:
            # Do not issue another POST. A caller may perform an explicit read-only lookup.
            raise
        if status != 200:
            raise DevTransportError("CHAT_SUBMIT_FAILED", status=status)
        # Bound/read SSE only to ensure the response stream has been consumed; events are
        # reloaded from the persisted Events API to support replay and de-duplication.
        text = raw.decode("utf-8", errors="replace")
        if len(text) > self.MAX_BODY:
            raise DevTransportError("SSE_RESPONSE_TOO_LARGE")
        run_id = next((value for key, value in headers.items() if key.casefold() == "x-run-id"), None)
        return {"run_id": run_id, "headers": {"X-Run-Id": run_id} if run_id else {},
                "status": next((value for key, value in headers.items() if key.casefold() == "x-run-status"), None)}

    def get_run(self, *, run_id: str) -> Mapping[str, Any]:
        self._require_auth()
        if not _API_ID.fullmatch(run_id):
            raise DevAuthorizationError("invalid Run ID")
        status, _, raw = self._request("GET", f"/api/runs/{run_id}")
        if status != 200:
            raise DevTransportError("RUN_READ_FAILED", status=status)
        return self._json(raw)

    def get_run_events(self, *, run_id: str, after_seq: int) -> Mapping[str, Any]:
        self._require_auth()
        if not _API_ID.fullmatch(run_id) or isinstance(after_seq, bool) or after_seq < 0:
            raise DevAuthorizationError("invalid Run event cursor")
        status, _, raw = self._request("GET", f"/api/runs/{run_id}/events?after_seq={int(after_seq)}")
        if status != 200:
            raise DevTransportError("RUN_EVENTS_READ_FAILED", status=status)
        return self._json(raw)

    def get_run_execution(self, *, run_id: str) -> Mapping[str, Any]:
        self._require_auth()
        if not _API_ID.fullmatch(run_id):
            raise DevAuthorizationError("invalid Run ID")
        status, _, raw = self._request("GET", f"/api/runs/{run_id}/execution")
        if status != 200:
            raise DevTransportError("RUN_EXECUTION_READ_FAILED", status=status)
        return self._json(raw)
