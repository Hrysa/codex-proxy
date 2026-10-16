#!/usr/bin/env python3
"""A token-protected Responses API gateway using the host's Codex configuration."""

from __future__ import annotations

import argparse
import base64
import copy
import hmac
from http.client import HTTPException
import ipaddress
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import tomllib
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

LOG = logging.getLogger("codex-proxy")
PROXY_KEY = "sk-codex-proxy"
MAX_BODY = 64 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
PRIVATE_HEADERS = {"authorization", "chatgpt-account-id", "cookie", "set-cookie",
                   "x-api-key", "api-key", "x-gateway-key", "openai-organization", "openai-project"}


class ConfigError(Exception):
    pass


class StreamError(Exception):
    pass


def error_identifiers(payload) -> tuple[str, str]:
    """Extract bounded error identifiers, never free-form messages or payloads."""
    if not isinstance(payload, dict):
        return "none", "none"
    if isinstance(payload.get("response"), dict):
        payload = payload["response"]
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return "none", "none"

    def identifier(value):
        if value is None:
            return "none"
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else "unknown"

    return identifier(error.get("code")), identifier(error.get("type"))


class SSEMonitor:
    """Observe framing and event types without logging conversation payloads."""

    def __init__(self):
        self.pending = b""
        self.events = 0
        self.last_event = "none"
        self.terminal = None
        self.error_code = "none"
        self.error_type = "none"
        self.failure_error = None

    def feed(self, chunk: bytes):
        self.pending = (self.pending + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self.pending:
            frame, self.pending = self.pending.split(b"\n\n", 1)
            data = []
            event = None
            for line in frame.removeprefix(b"\xef\xbb\xbf").split(b"\n"):
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode("utf-8", errors="replace")
                elif line.startswith(b"data:"):
                    data.append(line[5:].lstrip(b" "))
            if not data:
                continue
            self.events += 1
            payload = None
            try:
                payload = json.loads(b"\n".join(data))
                if isinstance(payload, dict):
                    event = payload.get("type") or event
            except (ValueError, UnicodeError):
                pass
            # Log only recognized event identifiers, never arbitrary payload text.
            self.last_event = event if isinstance(event, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", event) else "unknown"
            if self.last_event in {"response.completed", "response.failed", "error"} and isinstance(payload, dict) and payload.get("type") == self.last_event:
                self.terminal = self.last_event
                if self.terminal in {"response.failed", "error"}:
                    self.error_code, self.error_type = error_identifiers(payload)
                if self.terminal == "error":
                    error = payload.get("error", payload)
                    self.failure_error = dict(error) if isinstance(error, dict) else {
                        "message": "Upstream emitted an error event"}
        if len(self.pending) > MAX_BODY:
            raise StreamError("Upstream SSE event exceeds 64 MiB")


def response_chunks(upstream, encoding):
    decoder = None
    if encoding in {"gzip", "x-gzip"}:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    while True:
        chunk = upstream.read1(65536)
        if not chunk:
            break
        decoded = decoder.decompress(chunk) if decoder else chunk
        if decoded:
            yield decoded
    if decoder:
        decoded = decoder.flush()
        if decoded:
            yield decoded
        if not decoder.eof:
            raise StreamError("Compressed upstream response ended prematurely")


def stream_error_reason(error) -> str:
    if isinstance(error, StreamError):
        return str(error)
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(cause, socket.gaierror):
        return "Upstream DNS lookup failed; check the host's DNS and proxy settings"
    if isinstance(cause, ssl.SSLCertVerificationError):
        return "Upstream TLS certificate verification failed; check CODEX_CA_CERTIFICATE"
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return "Upstream connection or read timed out"
    if isinstance(cause, ConnectionRefusedError):
        return "Upstream connection refused; check the host's provider URL and HTTP proxy"
    if isinstance(cause, OSError):
        return f"Upstream socket error (errno={cause.errno}, type={type(cause).__name__})"
    return f"Upstream connection failed ({type(cause).__name__})"


def merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def read_config(home: Path, profile: str | None = None) -> dict:
    path = home / "config.toml"
    config = tomllib.loads(path.read_text()) if path.exists() else {}
    profile = profile or config.get("profile")
    if profile:
        if Path(profile).name != profile or profile in {".", ".."}:
            raise ConfigError("Invalid profile name")
        overlay = home / f"{profile}.config.toml"
        if overlay.exists():
            config = merge(config, tomllib.loads(overlay.read_text()))
        elif profile in config.get("profiles", {}):
            config = merge(config, config["profiles"][profile])
        else:
            raise ConfigError(f"Profile {profile!r} was not found")
    return config


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ConfigError("Base URL must be an HTTP(S) URL without credentials or a fragment")
    return url.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send host credentials to a redirect target.
        return None


class HostProvider:
    def __init__(self, home: Path, profile: str | None = None, codex: str = "codex"):
        self.home = home
        self.config = read_config(home, profile)
        self.name = self.config.get("model_provider", "openai")
        self.provider = self.config.get("model_providers", {}).get(self.name, {})
        if self.name != "openai" and not self.provider:
            raise ConfigError(f"Provider {self.name!r} is missing from model_providers")
        if self.provider.get("wire_api", "responses") != "responses":
            raise ConfigError("Only Responses API providers are supported")
        self.codex = codex
        self.refresh_lock = threading.Lock()
        self.last_refresh = 0.0
        self.command_token = None
        self.command_token_time = 0.0

    def _auth_file(self) -> dict:
        path = self.home / "auth.json"
        try:
            auth = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(auth, dict):
                raise ValueError
            return auth
        except (OSError, ValueError):
            raise ConfigError("Cannot read host auth.json; check the host's file-based Codex login") from None

    def model_catalog(self, query: str = "", timeout: float = 300) -> dict:
        configured = self.config.get("model_catalog_json")
        path = Path(configured).expanduser() if configured else self.home / "models_cache.json"
        if configured and not path.is_absolute():
            path = self.home / path
        def validated(catalog):
            models = catalog.get("models") if isinstance(catalog, dict) else None
            if not isinstance(models, list) or not models or any(
                not isinstance(model, dict) or not isinstance(model.get("slug"), str) for model in models
            ):
                raise ValueError
            # Cache and upstream envelopes can contain account identity fields.
            return {"models": models}

        try:
            return validated(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            if configured:
                raise ConfigError("Host configured model_catalog_json is unavailable or invalid; check that file") from None

        # Headless hosts may have credentials without ever creating models_cache.json.
        # Fetch metadata with host credentials; never invent context-window values.
        params = [(name, value) for name, value in parse_qsl(query) if name == "client_version"]
        upstream = self.request("/models", urlencode(params), None, {"Accept": "application/json"}, timeout)
        try:
            if upstream.status != 200:
                raise ConfigError(f"Host Codex model catalog is unavailable: upstream /models returned HTTP {upstream.status}")
            encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower() or "identity"
            if encoding not in {"identity", "gzip", "x-gzip", "deflate"}:
                raise ConfigError("Host Codex model catalog is unavailable: unsupported upstream encoding")
            data = bytearray()
            for chunk in response_chunks(upstream, encoding):
                if len(data) + len(chunk) > MAX_BODY:
                    raise ValueError
                data.extend(chunk)
            return validated(json.loads(data))
        except (ValueError, zlib.error, StreamError):
            raise ConfigError("Host Codex model catalog is unavailable: upstream /models did not return a valid Codex catalog. Set model_catalog_json to a valid host catalog") from None
        finally:
            upstream.close()

    def credentials(self) -> tuple[str, dict[str, str], bool]:
        p = self.provider
        headers = dict(p.get("http_headers", {}))
        for name, env in p.get("env_http_headers", {}).items():
            if os.environ.get(env):
                headers[name] = os.environ[env]
        needs_login = self.name == "openai" or p.get("requires_openai_auth", False)
        chatgpt = False
        if p.get("env_key"):
            token = os.environ.get(p["env_key"])
            if not token:
                raise ConfigError(f"Set the host environment variable {p['env_key']}")
        elif p.get("experimental_bearer_token"):
            token = p["experimental_bearer_token"]
        elif p.get("auth"):
            auth = p["auth"]
            with self.refresh_lock:
                interval = auth.get("refresh_interval_ms", 300000) / 1000
                if self.command_token is None or (interval > 0 and time.monotonic() - self.command_token_time >= interval):
                    try:
                        result = subprocess.run([auth["command"], *auth.get("args", [])], stdin=subprocess.DEVNULL,
                                                capture_output=True, text=True, check=True,
                                                timeout=auth.get("timeout_ms", 5000) / 1000)
                        self.command_token = result.stdout.strip()
                        self.command_token_time = time.monotonic()
                    except (OSError, subprocess.SubprocessError):
                        raise ConfigError("Provider credential command failed") from None
                    if not self.command_token:
                        raise ConfigError("Provider credential command returned an empty token")
                token = self.command_token
        elif needs_login:
            auth = self._auth_file()
            mode = auth.get("auth_mode")
            tokens = auth.get("tokens") or {}
            if mode in {"chatgpt", "chatgptAuthTokens"} or (mode is None and tokens.get("access_token")):
                token = tokens.get("access_token")
                chatgpt = True
                if tokens.get("account_id"):
                    headers["ChatGPT-Account-ID"] = tokens["account_id"]
            else:
                token = os.environ.get("OPENAI_API_KEY") or auth.get("OPENAI_API_KEY")
            if not token:
                raise ConfigError("No file-based Codex login. Run codex -c 'cli_auth_credentials_store=\"file\"' login on the host, or use a provider env_key")
        else:
            token = None  # Local unauthenticated providers are also supported.
        if token:
            headers["Authorization"] = f"Bearer {token}"
        base = p.get("base_url")
        if not base:
            if chatgpt:
                base = self.config.get("chatgpt_base_url", "https://chatgpt.com/backend-api")
                base = base.rstrip("/")
                if not base.endswith("/codex"):
                    base += "/codex"
            else:
                base = self.config.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return validate_url(base), headers, chatgpt

    def refresh(self, previous_authorization: str) -> None:
        """Let Codex own OAuth rotation and persistence; never implement a second OAuth client."""
        with self.refresh_lock:
            _, headers, chatgpt = self.credentials()
            if not chatgpt or headers.get("Authorization") != previous_authorization:
                return
            if time.monotonic() - self.last_refresh < 30:
                raise ConfigError("Host login refresh did not recover access; run codex login on the host")
            self.last_refresh = time.monotonic()
            refresh_login(self.codex, self.home)

    def request(self, path: str, query: str, body: bytes | None, client_headers, timeout: float):
        base, headers, chatgpt = self.credentials()
        if chatgpt and token_expired(headers.get("Authorization", "")):
            self.refresh(headers["Authorization"])
            base, headers, chatgpt = self.credentials()
        for attempt in range(2):
            outgoing = {}
            # Preserve protocol/session headers, never accept client credentials or routing.
            connection_headers = {v.strip().lower() for v in client_headers.get("Connection", "").split(",")}
            for name, value in client_headers.items():
                if name.lower() not in HOP_HEADERS | PRIVATE_HEADERS | connection_headers and name.lower() != "accept-encoding":
                    outgoing[name] = value
            outgoing.update(headers)
            outgoing["Accept-Encoding"] = "identity"
            if path == "/responses" and not any(name.lower() == "accept" for name in outgoing):
                try:
                    payload = json.loads(body) if body else None
                    if isinstance(payload, dict) and payload.get("stream") is True:
                        outgoing["Accept"] = "text/event-stream"
                except (ValueError, UnicodeError):
                    pass
            parts = urlsplit(base)
            params = dict(parse_qsl(query, keep_blank_values=True))
            params.update(self.provider.get("query_params", {}))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + path,
                              urlencode([*parse_qsl(parts.query, keep_blank_values=True), *params.items()]), ""))
            context = ssl.create_default_context(cafile=os.environ.get("CODEX_CA_CERTIFICATE"))
            opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
            try:
                return opener.open(Request(url, data=body, headers=outgoing), timeout=timeout)
            except HTTPError as error:
                if error.code == 401 and chatgpt and attempt == 0:
                    error.close()
                    self.refresh(headers.get("Authorization", ""))
                    base, headers, chatgpt = self.credentials()
                    continue
                if error.code == 401 and self.provider.get("auth") and attempt == 0:
                    error.close()
                    with self.refresh_lock:
                        if headers.get("Authorization") == f"Bearer {self.command_token}":
                            self.command_token = None
                    base, headers, chatgpt = self.credentials()
                    continue
                return error  # Preserve upstream status, body, and useful error headers.
        raise ConfigError("Upstream authentication failed")


def token_expired(authorization: str) -> bool:
    try:
        payload = authorization.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return isinstance(claims.get("exp"), (int, float)) and claims["exp"] < time.time() + 60
    except (IndexError, ValueError, TypeError):
        return False


def refresh_login(codex: str, home: Path, timeout: float = 30) -> None:
    """Use the documented app-server account/read refreshToken RPC."""
    env = {**os.environ, "CODEX_HOME": str(home)}
    try:
        process = subprocess.Popen([codex, "app-server", "-c", 'cli_auth_credentials_store="file"'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, env=env)
    except OSError:
        raise ConfigError("Cannot start codex to refresh host login; check --codex-bin") from None
    messages = queue.Queue()

    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(id):
        while True:
            message = messages.get(timeout=max(0, deadline - time.monotonic()))
            if message is None or message.get("id") == id:
                if message is None or "error" in message:
                    raise ConfigError("Codex could not refresh host login; run codex login on the host")
                return

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex_proxy", "version": "0.1.0"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/read", "params": {"refreshToken": True}})
        response(2)
    except (queue.Empty, OSError):
        raise ConfigError("Timed out or disconnected while refreshing host login") from None
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provider, timeout=300):
        self.provider = provider
        self.upstream_timeout = timeout
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexProxy/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        pass  # Request targets and headers can contain private data.

    def json_error(self, status, message):
        body = json.dumps({"error": {"message": message, "type": "proxy_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "15")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.proxy()

    def do_POST(self):
        self.proxy()

    def write_stream_chunk(self, chunk: bytes):
        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.wfile.flush()

    def write_stream_error(self, message: str):
        self.write_response_failure({"code": "proxy_incomplete_stream", "message": message})

    def write_response_failure(self, error: dict):
        # Codex's HTTP parser ignores generic `error` events, but classifies
        # response.failed and preserves its upstream error message and code.
        event = {"type": "response.failed", "response": {"status": "failed", "error": error}}
        self.write_stream_chunk(b"\n\nevent: response.failed\ndata: " + json.dumps(event).encode() + b"\n\n")

    def proxy(self):
        authorization = self.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization.encode(), f"Bearer {PROXY_KEY}".encode()):
            self.json_error(401, "Invalid proxy token")
            return
        parts = urlsplit(self.path)
        route = parts.path
        if route.startswith("/v1/"):
            route = route[3:]
        allowed = {"GET": {"/models", "/codex-models"}, "POST": {"/responses", "/responses/compact"}}
        if parts.scheme or parts.netloc or route not in allowed.get(self.command, set()):
            self.json_error(404, "Unsupported endpoint")
            return
        if self.headers.get("Upgrade"):
            self.json_error(400, "Use HTTP streaming; set supports_websockets = false in the client provider")
            return
        if self.headers.get("Transfer-Encoding"):
            self.json_error(400, "Send Content-Length instead of Transfer-Encoding")
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if lengths else 0
            if len(lengths) > 1 or length < 0:
                raise ValueError
        except ValueError:
            self.json_error(400, "Invalid Content-Length")
            return
        if length > MAX_BODY:
            self.json_error(413, "Request body exceeds 64 MiB")
            return
        started = False
        upstream = None
        monitor = None
        bytes_sent = 0
        request_id = "none"
        content_type = "unknown"
        encoding = "unknown"
        status = "unavailable"
        error_body = bytearray()
        try:
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                self.json_error(400, "Incomplete request body")
                return
            if self.command == "POST" and body is None:
                self.json_error(400, "Request body is required")
                return
            if self.command == "GET" and body is not None:
                self.json_error(400, "GET request bodies are unsupported")
                return
            if route == "/codex-models":
                catalog = json.dumps(self.server.provider.model_catalog(parts.query, self.server.upstream_timeout)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(catalog)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                started = True
                self.wfile.write(catalog)
                return
            upstream = self.server.provider.request(route, parts.query, body, self.headers, self.server.upstream_timeout)
            status = upstream.status if hasattr(upstream, "status") else upstream.code
            advertised_content_type = upstream.headers.get("Content-Type", "")
            content_type = advertised_content_type.split(";", 1)[0].strip().lower() or "missing"
            if content_type != "missing" and not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type):
                content_type = "unknown"
            encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower() or "identity"
            request_id = upstream.headers.get("X-Request-Id", "none")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
                request_id = "unavailable"
            if encoding not in {"identity", "gzip", "x-gzip", "deflate"}:
                safe_encoding = encoding if re.fullmatch(r"[a-z0-9, -]{1,80}", encoding) else "invalid"
                raise StreamError(f"Unsupported upstream Content-Encoding: {safe_encoding}; upstream must honor Accept-Encoding: identity")
            wants_stream = route == "/responses" and "text/event-stream" in self.headers.get("Accept", "")
            if route == "/responses" and body:
                try:
                    payload = json.loads(body)
                    wants_stream = wants_stream or (isinstance(payload, dict) and payload.get("stream") is True)
                except (ValueError, UnicodeError):
                    # Codex can send compressed request bodies; those are passed through.
                    wants_stream = wants_stream or bool(self.headers.get("Content-Encoding"))
            chunks = response_chunks(upstream, encoding)
            if status == 200 and wants_stream and content_type != "text/event-stream":
                # Some upstream gateways omit or mislabel Content-Type on valid SSE.
                # Inspect a bounded prefix and replay every byte after classification.
                prefix = b""
                while len(prefix) < 512 and b"\n" not in prefix:
                    chunk = next(chunks, b"")
                    if not chunk:
                        break
                    prefix += chunk
                if prefix.removeprefix(b"\xef\xbb\xbf").lstrip().startswith((b"data:", b"event:", b":")):
                    LOG.warning("Upstream SSE has content_type=%s; correcting response header", content_type)
                    content_type = "text/event-stream"
                    chunks = itertools.chain((prefix,), chunks)
                else:
                    raise StreamError(f"Upstream returned HTTP 200 without a text/event-stream response "
                                      f"(content_type={content_type}, encoding={encoding}, prefix_bytes={len(prefix)})")
            if status == 200 and route == "/responses" and content_type == "text/event-stream":
                monitor = SSEMonitor()
            self.send_response(status)
            connection_headers = {v.strip().lower() for v in upstream.headers.get("Connection", "").split(",")}
            for name, value in upstream.headers.items():
                if name.lower() not in HOP_HEADERS | PRIVATE_HEADERS | connection_headers | {"content-encoding", "content-type"}:
                    self.send_header(name, value)
            if content_type not in {"missing", "unknown"}:
                self.send_header("Content-Type", "text/event-stream" if monitor else advertised_content_type)
            if monitor:
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            started = True
            self.close_connection = True
            for chunk in chunks:
                if monitor:
                    monitor.feed(chunk)
                    self.write_stream_chunk(chunk)
                else:
                    if status >= 400 and len(error_body) <= 65536:
                        error_body.extend(chunk[:65537 - len(error_body)])
                    self.wfile.write(chunk)
                    self.wfile.flush()
                bytes_sent += len(chunk)
                if monitor and monitor.terminal:
                    break
            if monitor:
                # Some gateways omit the final blank line required to dispatch an SSE event.
                if monitor.pending.strip():
                    monitor.feed(b"\n\n")
                    self.write_stream_chunk(b"\n\n")
                if not monitor.terminal:
                    self.write_stream_error("Upstream closed before a terminal Responses event "
                                            f"(events={monitor.events}, last={monitor.last_event}, request_id={request_id}). "
                                            "Check the host proxy log for stream diagnostics.")
                elif monitor.terminal == "error":
                    # Keep the original event and append a failure Codex understands.
                    # Never turn an upstream error into a successful completion.
                    self.write_response_failure(monitor.failure_error)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                log = LOG.info if monitor.terminal == "response.completed" else LOG.warning
                log("%s %s -> %s bytes=%s events=%s last=%s terminal=%s encoding=%s request_id=%s error_code=%s error_type=%s",
                    self.command, route, status, bytes_sent, monitor.events, monitor.last_event,
                    monitor.terminal or "missing", encoding, request_id, monitor.error_code, monitor.error_type)
            else:
                error_code, error_type = "none", "none"
                if error_body and len(error_body) <= 65536:
                    try:
                        error_code, error_type = error_identifiers(json.loads(error_body))
                    except (ValueError, UnicodeError):
                        pass
                log = LOG.warning if status >= 400 else LOG.info
                log("%s %s -> %s bytes=%s encoding=%s request_id=%s error_code=%s error_type=%s",
                    self.command, route, status, bytes_sent, encoding, request_id, error_code, error_type)
        except ConfigError as error:
            LOG.warning("%s %s -> 503 Host configuration/authentication error reason=%s",
                        self.command, route, error)
            if not started:
                self.json_error(503, str(error))
        except (URLError, OSError, ValueError, HTTPException, StreamError, zlib.error) as error:
            reason = stream_error_reason(error)
            LOG.warning("%s %s Stream/connection failed (%s) status=%s bytes=%s events=%s last=%s content_type=%s encoding=%s request_id=%s reason=%s",
                        self.command, route, type(error).__name__, status, bytes_sent, monitor.events if monitor else 0,
                        monitor.last_event if monitor else "none", content_type, encoding, request_id, reason)
            if not started:
                try:
                    self.json_error(502, reason)
                except OSError:
                    pass
            elif monitor and not monitor.terminal and not isinstance(error, (BrokenPipeError, ConnectionResetError)):
                try:
                    self.write_stream_error(f"Upstream stream interrupted ({type(error).__name__}, request_id={request_id})")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()
            self.close_connection = True


def client_config(model: str, base_url: str) -> str:
    parts = urlsplit(base_url)
    catalog_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/codex-models", parts.query, ""))
    return (f'model = {json.dumps(model)}\nmodel_provider = "codex_proxy"\n\n'
            '[model_providers.codex_proxy]\nname = "Host Codex proxy"\n'
            f'base_url = {json.dumps(base_url)}\nexperimental_bearer_token = {json.dumps(PROXY_KEY)}\n'
            f'model_catalog_url = {json.dumps(catalog_url)}\n'
            'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false')


def detect_lan_ip() -> str:
    def usable(address):
        try:
            ip = ipaddress.IPv4Address(address)
            return not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved)
        except ValueError:
            return False

    try:
        # UDP connect selects the outbound interface without transmitting a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            address = probe.getsockname()[0]
            if usable(address):
                return address
    except OSError:
        pass
    try:
        candidates = [info[4][0] for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
        ) if usable(info[4][0])]
        if candidates:
            return next((address for address in candidates if ipaddress.IPv4Address(address).is_private), candidates[0])
    except OSError:
        pass
    raise ConfigError("Could not detect the host's LAN IPv4 address. Connect the host to a network and retry")


def print_client_usage(address, model: str | None) -> None:
    _, port = address
    base_url = f"http://{detect_lan_ip()}:{port}/v1"
    lines = ["", "Client setup", f"API base URL: {base_url}", f"API key: {PROXY_KEY}", ""]
    lines.extend(["Merge this into the client's ~/.codex/config.toml (or $CODEX_HOME/config.toml).",
                  "Put model and model_provider before tables; replace existing keys instead of duplicating them."])
    if not model:
        lines.append("Replace YOUR_HOST_MODEL with a model available to the host account.")
    lines.extend(["", "```toml", client_config(model or "YOUR_HOST_MODEL", base_url), "```", "",
                  "Then run on the client:", "  codex", "",
                  "If the client still reports missing model metadata, download the host catalog:",
                  '  mkdir -p "$HOME/.codex"',
                  f"  curl -fSs -H 'Authorization: Bearer {PROXY_KEY}' '{base_url}/codex-models' "
                  '-o "$HOME/.codex/proxy-models.json"',
                  'Add model_catalog_json = "/absolute/path/to/.codex/proxy-models.json" before TOML tables,',
                  "using the downloaded file's absolute path, then restart Codex.", "",
                  "No client login or token environment variable is required. Tools run on the client.", ""])
    print("\n".join(lines), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--profile", help="Host Codex profile (new file or legacy profiles table)")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Expose the host's model provider")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--upstream-timeout", type=float, default=300)
    serve.add_argument("--codex-bin", default="codex", help="Codex executable for login renewal")
    client = commands.add_parser("client-config", help="Print a standard client config.toml snippet")
    client.add_argument("--base-url", help="Defaults to the detected LAN IP on port 8787")
    client.add_argument("--model", help="Defaults to the host config model")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "client-config":
            model = args.model or read_config(args.codex_home, args.profile).get("model")
            if not model:
                raise ConfigError("Supply --model or set model in the host config.toml")
            base = validate_url(args.base_url or f"http://{detect_lan_ip()}:8787/v1")
            print(client_config(model, base))
            return 0
        if args.upstream_timeout <= 0:
            raise ConfigError("--upstream-timeout must be positive")
        provider = HostProvider(args.codex_home, args.profile, args.codex_bin)
        provider.credentials()  # Fail at startup if login/configuration is missing.
        server = ProxyServer(("0.0.0.0", args.port), provider, args.upstream_timeout)
        LOG.info("Listening on %s:%s; host provider: %s", *server.server_address, provider.name)
        try:
            print_client_usage(server.server_address, provider.config.get("model"))
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ConfigError, OSError, ValueError) as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
