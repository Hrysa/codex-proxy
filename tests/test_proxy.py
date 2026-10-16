import base64
import gzip
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import itertools
import zlib
from unittest.mock import Mock, patch

import codex_proxy as proxy


TOKEN = proxy.PROXY_KEY
SOCKETPAIR = os.environ.get("CODEX_PROXY_TEST_TRANSPORT") == "socketpair"
SERVERS = {}
PORTS = itertools.count(18000)


def create_server(server_class, *args):
    if not SOCKETPAIR:
        return server_class(*args)

    def bind(server):
        # Exercise real HTTP handlers without binding any network interface.
        server.server_address = ("127.0.0.1", next(PORTS))
        server.server_name, server.server_port = server.server_address

    with patch.object(ThreadingHTTPServer, "server_bind", bind), patch.object(ThreadingHTTPServer, "server_activate"):
        server = server_class(*args)
    SERVERS[server.server_address[1]] = server
    return server


def memory_connection(address, timeout=None, source_address=None, **kwargs):
    server = SERVERS.get(int(address[1]))
    if server is None:
        raise ConnectionRefusedError("No mock server at this address")
    client, accepted = socket.socketpair()
    if timeout is not None and isinstance(timeout, (int, float)):
        client.settimeout(timeout)
    server.process_request(accepted, ("127.0.0.1", 1))
    class SocketPairClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def setsockopt(self, level, option, value):
            if level != socket.IPPROTO_TCP:
                client.setsockopt(level, option, value)

    return SocketPairClient()


@contextmanager
def running(server):
    if SOCKETPAIR:
        try:
            yield server.server_address[1]
        finally:
            SERVERS.pop(server.server_address[1], None)
            server.server_close()
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Expected when testing client cancellation or header rejection.

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.path, dict(self.headers), body))
        status = self.server.status
        self.send_response(status)
        if self.server.content_type is not None:
            self.send_header("Content-Type", self.server.content_type)
        if self.server.encoding:
            self.send_header("Content-Encoding", self.server.encoding)
        if self.server.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Request-Id", "upstream-request")
        self.send_header("Set-Cookie", "private-cookie=secret")
        if status == 307:
            self.send_header("Location", self.server.redirect)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.server.custom_body is not None:
            self.wfile.write(self.server.custom_body)
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(b'data: {"type":"response.created"}\n\n')
        self.wfile.flush()
        if self.server.pause:
            self.server.release.wait(timeout=3)
        self.wfile.write(b'data: {"type":"response.completed"}\n\n')
        self.wfile.flush()
        self.close_connection = True


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        if SOCKETPAIR:
            self.enterContext(patch("socket.create_connection", memory_connection))
        self.up = create_server(ThreadingHTTPServer, ("127.0.0.1", 0), Upstream)
        self.up.requests = []
        self.up.status = 200
        self.up.pause = False
        self.up.content_type = "text/event-stream"
        self.up.encoding = None
        self.up.chunked = False
        self.up.custom_body = None
        self.up.release = threading.Event()
        self.up.redirect = "http://127.0.0.1:1/leak"
        self.enterContext(running(self.up))
        self.base = f"http://127.0.0.1:{self.up.server_address[1]}/backend-api/codex"
        (self.home / "config.toml").write_text(f'model = "host-model"\nchatgpt_base_url = "{self.base}"\n')
        self.auth("host-access")
        self.env = self.enterContext(patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}))
        self.provider = proxy.HostProvider(self.home)
        self.port = self.enterContext(running(create_server(proxy.ProxyServer, ("127.0.0.1", 0), self.provider)))

    def auth(self, access):
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": access, "account_id": "host-account", "refresh_token": "host-refresh"}}))

    def request(self, method="POST", path="/v1/responses", body=b'{"stream":true}', headers=None, token=TOKEN):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        outgoing = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        outgoing.update(headers or {})
        connection.request(method, path, body=body, headers=outgoing)
        return connection.getresponse()

    def test_client_token_replaced_and_tools_preserved(self):
        body = json.dumps({"model": "host-model", "stream": True, "tools": [{"type": "function", "name": "shell"}],
                           "input": [{"type": "function_call_output", "call_id": "1", "output": "hello"}]}).encode()
        response = self.request(body=body, headers={"ChatGPT-Account-ID": "client-account", "Cookie": "client-secret",
                                                   "X-Api-Key": "client-key", "Session-Id": "session-1"})
        self.assertEqual(response.status, 200)
        self.assertIn(b"response.completed", response.read())
        path, headers, received = self.up.requests[0]
        self.assertEqual(path, "/backend-api/codex/responses")
        self.assertEqual(received, body)
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(headers["authorization"], "Bearer host-access")
        self.assertEqual(headers["chatgpt-account-id"], "host-account")
        self.assertEqual(headers["session-id"], "session-1")
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertEqual(response.getheader("X-Request-Id"), "upstream-request")

    def test_stream_arrives_before_completion(self):
        self.up.pause = True
        response = self.request()
        try:
            self.assertEqual(response.readline(), b'data: {"type":"response.created"}\n')
            self.assertFalse(self.up.release.is_set())
        finally:
            self.up.release.set()
        self.assertIn(b"response.completed", response.read())

    def test_compressed_sse_is_decoded_and_completed(self):
        payload = (b'event: response.created\r\ndata: {"type":"response.created"}\r\n\r\n'
                   b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","output":[]}}\r\n\r\n')
        for encoding, compress in [("gzip", gzip.compress), ("deflate", zlib.compress)]:
            with self.subTest(encoding=encoding):
                self.up.encoding = encoding
                self.up.custom_body = compress(payload)
                response = self.request()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertEqual(response.read(), payload)

    def test_incomplete_sse_emits_error_and_diagnostic_instead_of_success(self):
        self.up.custom_body = b'data: {"type":"response.created"}\n\n'
        with self.assertLogs(proxy.LOG, level="WARNING") as logs:
            response = self.request()
            body = response.read()
        self.assertIn(b'"code": "proxy_incomplete_stream"', body)
        self.assertIn(b'"type": "response.failed"', body)
        self.assertNotIn(b'"type":"response.completed"', body)
        self.assertIn("terminal=missing", logs.output[0])
        self.assertIn("last=response.created", logs.output[0])
        self.assertIn("request_id=upstream-request", logs.output[0])

    def test_completed_event_missing_final_blank_line_is_dispatched(self):
        self.up.custom_body = b'data: {"type":"response.completed","response":{"id":"resp_test","status":"completed","output":[]}}'
        response = self.request()
        body = response.read()
        self.assertEqual(body, self.up.custom_body + b"\n\n")
        self.assertNotIn(b"proxy_incomplete_stream", body)

    def test_truncated_http_chunk_becomes_explicit_stream_error(self):
        self.up.chunked = True
        self.up.custom_body = b'100\r\ndata: {"type":"response.created"}\n\n'
        response = self.request()
        body = response.read()
        self.assertIn(b"proxy_incomplete_stream", body)
        self.assertIn(b"IncompleteRead", body)

    def test_chunked_upstream_is_dechunked_and_reframed(self):
        self.up.chunked = True
        payload = b'data: {"type":"response.completed"}\n\n'
        self.up.custom_body = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
        self.assertEqual(self.request().read(), payload)

    def test_success_status_with_html_is_not_a_successful_stream(self):
        self.up.content_type = "text/html"
        self.up.custom_body = b"<html>Sign in required</html>"
        response = self.request()
        self.assertEqual(response.status, 502)
        self.assertIn(b"without a text/event-stream", response.read())

    def test_missing_or_mislabelled_content_type_does_not_drop_valid_sse(self):
        payload = b'data: {"type":"response.created"}\n\ndata: {"type":"response.completed"}\n\n'
        for content_type in (None, "text/plain", "unrecognized"):
            with self.subTest(content_type=content_type):
                self.up.content_type = content_type
                self.up.custom_body = payload
                response = self.request()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
                self.assertEqual(response.read(), payload)

    def test_compressed_sse_without_content_type_is_detected_after_decoding(self):
        payload = b'data: {"type":"response.completed","response":{"id":"resp_test","status":"completed","output":[]}}\n\n'
        self.up.content_type = None
        self.up.encoding = "gzip"
        self.up.custom_body = gzip.compress(payload)
        response = self.request()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(response.read(), payload)

    def test_empty_200_without_headers_logs_actual_reason_and_status(self):
        self.up.content_type = None
        self.up.custom_body = b""
        with self.assertLogs(proxy.LOG, level="WARNING") as logs:
            response = self.request()
            self.assertEqual(response.status, 502)
            body = response.read()
        self.assertIn(b"content_type=missing", body)
        self.assertIn("status=200", logs.output[0])
        self.assertIn("encoding=identity", logs.output[0])
        self.assertIn("reason=Upstream returned HTTP 200", logs.output[0])
        self.assertIn("prefix_bytes=0", logs.output[0])

    def test_unsupported_encoding_logs_encoding_and_actionable_reason(self):
        self.up.encoding = "unsupported-test-codec"
        with self.assertLogs(proxy.LOG, level="WARNING") as logs:
            response = self.request()
            self.assertEqual(response.status, 502)
            body = response.read()
        self.assertIn(b"Unsupported upstream Content-Encoding: unsupported-test-codec", body)
        self.assertIn("reason=Unsupported upstream Content-Encoding: unsupported-test-codec", logs.output[0])

    def test_upstream_failure_event_is_preserved(self):
        self.up.custom_body = b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"test_error","message":"test failure"}}}\n\n'
        with self.assertLogs(proxy.LOG, level="WARNING") as logs:
            response = self.request()
            self.assertEqual(response.read(), self.up.custom_body)
        self.assertIn("error_code=test_error", logs.output[-1])
        self.assertNotIn("test failure", "\n".join(logs.output))

    def test_headerless_sse_error_logs_code_without_message_or_retry(self):
        self.up.content_type = None
        for error in [
            {"type": "error", "code": "context_length_exceeded", "message": "private conversation"},
            {"type": "error", "error": {"code": "invalid_encrypted_content",
                                        "type": "invalid_request_error", "message": "private conversation"}},
        ]:
            with self.subTest(error=error):
                self.up.custom_body = b'data: ' + json.dumps(error).encode() + b'\n\n'
                before = len(self.up.requests)
                with self.assertLogs(proxy.LOG, level="WARNING") as logs:
                    response = self.request()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
                    body = response.read()
                    self.assertTrue(body.startswith(self.up.custom_body))
                    failure = json.loads(body.split(b"data: ")[-1].strip())
                    self.assertEqual(failure["type"], "response.failed")
                    self.assertEqual(failure["response"]["status"], "failed")
                    self.assertEqual(failure["response"]["error"], error.get("error", error))
                    self.assertNotIn(b"response.completed", body)
                expected_code = error.get("error", error)["code"]
                self.assertIn(f"error_code={expected_code}", logs.output[-1])
                self.assertIn("terminal=error", logs.output[-1])
                self.assertNotIn("private conversation", "\n".join(logs.output))
                self.assertEqual(len(self.up.requests), before + 1)

    def test_nested_internal_error_without_final_delimiter_is_visible_to_codex(self):
        error = {"type": "internal_error", "message": "Upstream could not process this history"}
        self.up.custom_body = b'data: ' + json.dumps({"type": "error", "error": error}).encode()
        body = self.request().read()
        self.assertTrue(body.startswith(self.up.custom_body + b"\n\n"))
        failed = json.loads(body.split(b"data: ")[-1].strip())
        self.assertEqual(failed["type"], "response.failed")
        self.assertEqual(failed["response"]["error"], error)
        self.assertEqual(body.count(b'"type": "response.failed"'), 1)
        self.assertNotIn(b"response.completed", body)

    def test_nonstreaming_json_response_still_passes_through(self):
        self.up.content_type = "application/json"
        self.up.custom_body = b'{"id":"resp_test","status":"completed"}'
        response = self.request(body=b'{"stream":false}')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), self.up.custom_body)

    def test_invalid_token_never_contacts_upstream(self):
        response = self.request(token="wrong")
        self.assertEqual(response.status, 401)
        self.assertIn(b"Invalid proxy token", response.read())
        self.assertEqual(self.up.requests, [])

    def test_health_does_not_reveal_provider_or_token(self):
        response = self.request("GET", "/healthz", None, token="")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"status": "ok"})

    def test_auth_file_reloaded(self):
        self.request().read()
        self.auth("rotated-access")
        self.request().read()
        self.assertEqual(self.up.requests[-1][1]["Authorization"], "Bearer rotated-access")

    def test_models_compaction_and_query(self):
        for method, route, body in [("GET", "/models?client_version=1", None),
                                    ("POST", "/v1/responses/compact", b'{}')]:
            response = self.request(method, route, body)
            self.assertEqual(response.status, 200)
            response.read()
        self.assertTrue(self.up.requests[0][0].endswith("/models?client_version=1"))
        self.assertTrue(self.up.requests[1][0].endswith("/responses/compact"))

    def test_compaction_json_and_followup_preserve_encrypted_items(self):
        body = b'{"model":"host-model","input":[{"role":"user","content":"private history"}]}'
        compacted = {"id": "cmp_test", "object": "response.compaction", "output": [
            {"type": "compaction", "encrypted_content": "opaque-private-content"}]}
        self.up.content_type = "application/json"
        self.up.encoding = "gzip"
        payload = json.dumps(compacted).encode()
        self.up.custom_body = gzip.compress(payload)
        response = self.request(path="/v1/responses/compact", body=body)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(response.read(), payload)
        self.assertEqual(self.up.requests[-1][2], body)
        self.assertEqual(self.up.requests[-1][0], "/backend-api/codex/responses/compact")
        self.up.content_type, self.up.encoding, self.up.custom_body = "text/event-stream", None, None
        followup = json.dumps({"model": "host-model", "stream": True, "input": compacted["output"]}).encode()
        self.assertIn(b"response.completed", self.request(body=followup).read())
        self.assertEqual(self.up.requests[-1][2], followup)

    def test_compaction_http_error_is_logged_and_preserved(self):
        self.up.status = 400
        self.up.content_type = "application/json"
        self.up.encoding = "gzip"
        payload = b'{"error":{"code":"context_length_exceeded","type":"invalid_request_error","message":"private history"}}'
        self.up.custom_body = gzip.compress(payload)
        with self.assertLogs(proxy.LOG, level="WARNING") as logs:
            response = self.request(path="/v1/responses/compact", body=b'{}')
            self.assertEqual(response.status, 400)
            self.assertEqual(response.read(), payload)
        self.assertIn("POST /responses/compact -> 400", logs.output[-1])
        self.assertIn("error_code=context_length_exceeded error_type=invalid_request_error", logs.output[-1])
        self.assertNotIn("private history", "\n".join(logs.output))

    def test_compaction_refresh_failure_logs_route_and_reason(self):
        self.up.status = 401
        with patch.object(proxy, "refresh_login", side_effect=proxy.ConfigError("Cannot start codex to refresh host login; check --codex-bin")):
            with self.assertLogs(proxy.LOG, level="WARNING") as logs:
                response = self.request(path="/v1/responses/compact", body=b'{}')
                self.assertEqual(response.status, 503)
                response.read()
        self.assertIn("POST /responses/compact -> 503", logs.output[-1])
        self.assertIn("reason=Cannot start codex", logs.output[-1])
        self.assertNotIn("host-access", "\n".join(logs.output))

    def test_host_catalog_preserves_metadata_without_cache_identity(self):
        models = [{"slug": "host-model", "context_window": 500000, "supported_reasoning_levels": []}]
        (self.home / "models_cache.json").write_text(json.dumps({"models": models,
                                                                "identity": "private-account", "etag": "private-cache"}))
        response = self.request("GET", "/v1/codex-models?client_version=0.153.2", None)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"models": models})
        self.assertEqual(self.up.requests, [])

    def test_host_catalog_requires_proxy_key(self):
        response = self.request("GET", "/v1/codex-models", None, token="wrong")
        self.assertEqual(response.status, 401)
        response.read()

    def test_missing_host_catalog_is_actionable(self):
        response = self.request("GET", "/v1/codex-models", None)
        self.assertEqual(response.status, 503)
        self.assertIn(b"Host Codex model catalog is unavailable", response.read())

    def test_missing_catalog_fetches_upstream_metadata_with_host_credentials(self):
        models = [{"slug": "host-model", "context_window": 272000, "auto_compact_token_limit": 240000}]
        self.up.content_type = "application/json"
        self.up.encoding = "gzip"
        self.up.custom_body = gzip.compress(json.dumps({"models": models, "identity": "private-account"}).encode())
        response = self.request("GET", "/v1/codex-models?client_version=0.162.1&ignored=private", None)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"models": models})
        path, headers, body = self.up.requests[-1]
        self.assertEqual(path, "/backend-api/codex/models?client_version=0.162.1")
        self.assertEqual(headers["Authorization"], "Bearer host-access")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(body, b"")
        self.assertFalse((self.home / "models_cache.json").exists())

    def test_upstream_catalog_rejects_generic_model_list(self):
        self.up.content_type = "application/json"
        self.up.custom_body = b'{"data":[{"id":"host-model"}]}'
        response = self.request("GET", "/v1/codex-models", None)
        self.assertEqual(response.status, 503)
        self.assertIn(b"valid Codex catalog", response.read())

    def test_catalog_upstream_http_error_is_not_accepted_as_metadata(self):
        self.up.status = 429
        response = self.request("GET", "/v1/codex-models", None)
        self.assertEqual(response.status, 503)
        self.assertIn(b"upstream /models returned HTTP 429", response.read())

    def test_invalid_explicit_catalog_does_not_silently_change_source(self):
        self.provider.config["model_catalog_json"] = "missing-explicit.json"
        response = self.request("GET", "/v1/codex-models", None)
        self.assertEqual(response.status, 503)
        self.assertIn(b"configured model_catalog_json", response.read())
        self.assertEqual(self.up.requests, [])

    def test_explicit_host_catalog_takes_priority_over_cache(self):
        (self.home / "explicit.json").write_text(json.dumps({"models": [{"slug": "explicit-model"}]}))
        (self.home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "cached-model"}]}))
        self.provider.config["model_catalog_json"] = "explicit.json"
        self.assertEqual(self.provider.model_catalog(), {"models": [{"slug": "explicit-model"}]})

    def test_errors_pass_through(self):
        self.up.status = 429
        response = self.request()
        self.assertEqual(response.status, 429)
        self.assertIn(b"response.completed", response.read())

    def test_redirect_is_not_followed(self):
        self.up.status = 307
        response = self.request()
        self.assertEqual(response.status, 307)
        response.read()
        self.assertEqual(len(self.up.requests), 1)

    def test_401_renews_host_login_and_retries_once(self):
        self.up.status = 401
        def refresh(codex, home):
            self.auth("renewed-access")
            self.up.status = 200
        with patch.object(proxy, "refresh_login", side_effect=refresh) as renew:
            response = self.request()
            self.assertEqual(response.status, 200)
            response.read()
            renew.assert_called_once()
        self.assertEqual(len(self.up.requests), 2)
        self.assertEqual(self.up.requests[1][1]["Authorization"], "Bearer renewed-access")

    def test_refresh_failure_is_actionable(self):
        self.up.status = 401
        with patch.object(proxy, "refresh_login", side_effect=proxy.ConfigError("run codex login on the host")):
            response = self.request()
            self.assertEqual(response.status, 503)
            self.assertIn(b"run codex login", response.read())

    def test_expired_token_renews_before_request(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() - 10}).encode()).decode().rstrip("=")
        self.auth(f"header.{payload}.signature")
        with patch.object(proxy, "refresh_login", side_effect=lambda *a: self.auth("renewed-access")) as renew:
            self.request().read()
            renew.assert_called_once()
        self.assertEqual(len(self.up.requests), 1)

    def test_unknown_endpoints_and_absolute_targets_rejected(self):
        for route in ["/auth.json", "/shutdown", "/v1/responses/../models", "http://example.com/v1/responses"]:
            response = self.request(path=route)
            self.assertEqual(response.status, 404)
            response.read()
        self.assertEqual(self.up.requests, [])

    def test_request_size_and_transfer_encoding(self):
        response = self.request(headers={"Content-Length": str(proxy.MAX_BODY + 1)})
        self.assertEqual(response.status, 413)
        response.read()
        response = self.request(headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(response.status, 400)
        response.read()
        self.assertEqual(self.up.requests, [])

    def test_custom_provider_and_host_headers(self):
        (self.home / "config.toml").write_text(f'''model_provider = "custom"
[model_providers.custom]
base_url = "{self.base}?existing=yes"
env_key = "TEST_HOST_KEY"
http_headers = {{ "X-Host" = "configured" }}
env_http_headers = {{ "X-Environment" = "TEST_HOST_HEADER" }}
query_params = {{ "api-version" = "host-version" }}
''')
        with patch.dict(os.environ, {"TEST_HOST_KEY": "provider-key", "TEST_HOST_HEADER": "from-env"}):
            self.provider = proxy.HostProvider(self.home)
            # The existing HTTP server keeps the provider object; replace its configuration.
            port = self.enterContext(running(create_server(proxy.ProxyServer, ("127.0.0.1", 0), self.provider)))
            self.port = port
            self.request(path="/v1/responses?api-version=client-version").read()
        path, headers, _ = self.up.requests[0]
        self.assertIn("api-version=host-version", path)
        self.assertIn("existing=yes", path)
        self.assertEqual(headers["Authorization"], "Bearer provider-key")
        self.assertEqual(headers["X-Host"], "configured")
        self.assertEqual(headers["X-Environment"], "from-env")
        self.assertNotIn("Chatgpt-Account-Id", headers)

    def test_apikey_login(self):
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "host-api-key"}))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            base, headers, chatgpt = self.provider.credentials()
        self.assertEqual(base, "https://api.openai.com/v1")
        self.assertEqual(headers["Authorization"], "Bearer host-api-key")
        self.assertFalse(chatgpt)

    def test_profile_formats(self):
        (self.home / "work.config.toml").write_text('model = "profile-model"\n')
        self.assertEqual(proxy.read_config(self.home, "work")["model"], "profile-model")
        with (self.home / "config.toml").open("a") as f:
            f.write('[profiles.legacy]\nmodel = "legacy-model"\n')
        self.assertEqual(proxy.read_config(self.home, "legacy")["model"], "legacy-model")

    def test_client_config_is_valid_toml_and_uses_host_model(self):
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(proxy, "detect_lan_ip", return_value="192.168.1.10"):
            self.assertEqual(proxy.main(["--codex-home", str(self.home), "client-config"]), 0)
        config = tomllib.loads(output.getvalue())
        self.assertEqual(config["model"], "host-model")
        provider = config["model_providers"][config["model_provider"]]
        self.assertEqual(provider["experimental_bearer_token"], TOKEN)
        self.assertEqual(provider["base_url"], "http://192.168.1.10:8787/v1")
        self.assertEqual(provider["model_catalog_url"], "http://192.168.1.10:8787/v1/codex-models")
        self.assertNotIn("env_key", provider)
        self.assertFalse(provider["supports_websockets"])
        self.assertFalse(provider["requires_openai_auth"])

    def test_serve_prints_client_setup_with_bound_port(self):
        server = Mock(server_address=("0.0.0.0", 43210))
        server.serve_forever.side_effect = KeyboardInterrupt
        output = io.StringIO()
        with patch.object(proxy, "ProxyServer", return_value=server) as constructor, \
                patch.object(proxy, "detect_lan_ip", return_value="192.168.1.10"), patch("sys.stdout", output):
            self.assertEqual(proxy.main(["--codex-home", str(self.home), "serve", "--port", "0"]), 0)
        self.assertEqual(constructor.call_args.args[0], ("0.0.0.0", 0))
        text = output.getvalue()
        snippet = text.split("```toml\n", 1)[1].split("```", 1)[0]
        config = tomllib.loads(snippet)
        self.assertEqual(config["model"], "host-model")
        provider = config["model_providers"]["codex_proxy"]
        self.assertEqual(provider["experimental_bearer_token"], TOKEN)
        self.assertEqual(provider["base_url"], "http://192.168.1.10:43210/v1")
        self.assertEqual(provider["model_catalog_url"], "http://192.168.1.10:43210/v1/codex-models")
        self.assertIn("~/.codex/config.toml", text)
        self.assertIn("Then run on the client:\n  codex", text)
        self.assertNotIn("http://HOST", text)
        self.assertIn("'http://192.168.1.10:43210/v1/codex-models'", text)
        server.server_close.assert_called_once()

    def test_missing_login_is_clear(self):
        (self.home / "auth.json").unlink()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(proxy.ConfigError, "No file-based Codex login"):
                self.provider.credentials()

    def test_corrupt_auth_file_is_actionable(self):
        (self.home / "auth.json").write_text("invalid json")
        response = self.request()
        self.assertEqual(response.status, 503)
        self.assertIn(b"Cannot read host auth.json", response.read())

    def test_credential_command_retries_after_401(self):
        (self.home / "config.toml").write_text(f'''model_provider = "custom"
[model_providers.custom]
base_url = "{self.base}"
[model_providers.custom.auth]
command = "fake-helper"
refresh_interval_ms = 0
''')
        self.port = self.enterContext(running(create_server(proxy.ProxyServer, ("127.0.0.1", 0),
                                                          proxy.HostProvider(self.home))))
        self.up.status = 401
        def helper(*args, **kwargs):
            if len(self.up.requests):
                self.up.status = 200
                return proxy.subprocess.CompletedProcess(args, 0, stdout="renewed-helper-token\n")
            return proxy.subprocess.CompletedProcess(args, 0, stdout="original-helper-token\n")
        with patch.object(proxy.subprocess, "run", side_effect=helper) as command:
            response = self.request()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertEqual(command.call_count, 2)
        self.assertEqual(self.up.requests[1][1]["Authorization"], "Bearer renewed-helper-token")

    def test_client_disconnect_does_not_break_next_request(self):
        connection = socket.create_connection(("127.0.0.1", self.port))
        connection.sendall(f"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n\r\n{{}}".encode())
        connection.close()
        response = self.request()
        self.assertEqual(response.status, 200)
        response.read()


class StreamFramingTests(unittest.TestCase):
    def test_error_identifiers_reject_freeform_and_malformed_fields(self):
        for payload in [None, [], {"error": "private message"}]:
            self.assertEqual(proxy.error_identifiers(payload), ("none", "none"))
        self.assertEqual(proxy.error_identifiers({"error": {"code": "private\nmessage", "type": {"secret": 1}}}),
                         ("unknown", "unknown"))
        self.assertEqual(proxy.error_identifiers({"code": "x" * 101}), ("unknown", "none"))

    def test_dns_failure_reason_is_specific_without_logging_arbitrary_exception_text(self):
        error = proxy.URLError(socket.gaierror(-8, "private-diagnostic-data"))
        reason = proxy.stream_error_reason(error)
        self.assertIn("DNS lookup failed", reason)
        self.assertNotIn("private-diagnostic-data", reason)

    def test_utf8_bom_does_not_hide_first_completed_event(self):
        monitor = proxy.SSEMonitor()
        monitor.feed(b'\xef\xbb\xbfdata: {"type":"response.completed"}\n\n')
        self.assertEqual(monitor.terminal, "response.completed")
    def test_crlf_and_json_can_be_split_at_every_byte_boundary(self):
        monitor = proxy.SSEMonitor()
        payload = b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","output":[]}}\r\n\r\n'
        for value in payload:
            monitor.feed(bytes([value]))
        self.assertEqual(monitor.terminal, "response.completed")
        self.assertEqual(monitor.events, 1)

    def test_completion_text_inside_a_delta_is_not_a_terminal_event(self):
        monitor = proxy.SSEMonitor()
        monitor.feed(b'data: {"type":"response.output_text.delta","delta":"response.completed"}\n\n')
        self.assertIsNone(monitor.terminal)
        self.assertEqual(monitor.last_event, "response.output_text.delta")

    def test_named_completion_with_invalid_payload_is_not_success(self):
        monitor = proxy.SSEMonitor()
        monitor.feed(b'event: response.completed\ndata: invalid json\n\n')
        self.assertIsNone(monitor.terminal)


class LanAddressTests(unittest.TestCase):
    def test_uses_default_route_address_without_sending_data(self):
        with patch.object(proxy.socket, "socket") as factory, patch.object(proxy.socket, "getaddrinfo") as lookup:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("192.168.1.10", 12345)
            self.assertEqual(proxy.detect_lan_ip(), "192.168.1.10")
            probe.connect.assert_called_once_with(("192.0.2.1", 80))
            probe.send.assert_not_called()
            probe.sendto.assert_not_called()
            lookup.assert_not_called()

    def test_hostname_fallback_ignores_loopback(self):
        addresses = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (ip, 0))
                     for ip in ("127.0.0.1", "0.0.0.0", "192.168.1.20")]
        with patch.object(proxy.socket, "socket", side_effect=OSError), \
                patch.object(proxy.socket, "getaddrinfo", return_value=addresses):
            self.assertEqual(proxy.detect_lan_ip(), "192.168.1.20")

    def test_reports_detection_failure_instead_of_a_placeholder(self):
        with patch.object(proxy.socket, "socket", side_effect=OSError), \
                patch.object(proxy.socket, "getaddrinfo", side_effect=OSError):
            with self.assertRaisesRegex(proxy.ConfigError, "Could not detect"):
                proxy.detect_lan_ip()


class RefreshProtocolTests(unittest.TestCase):
    def test_real_subprocess_rpc_handshake_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "fake-codex"
            executable.write_text(f'''#!{sys.executable}
import json,sys
assert sys.argv[1:] == ['app-server', '-c', 'cli_auth_credentials_store="file"']
message = json.loads(sys.stdin.readline())
assert message['method'] == 'initialize'
print(json.dumps({{'id':1,'result':{{}}}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
message = json.loads(sys.stdin.readline())
assert message['method'] == 'account/read' and message['params']['refreshToken'] is True
print(json.dumps({{'id':2,'result':{{'account':{{'type':'chatgpt'}}}}}}), flush=True)
sys.stdin.read()
''')
            executable.chmod(0o700)
            # Windows cannot execute a shebang script directly. Still exercise a
            # real subprocess and the same RPC protocol on every platform.
            popen = proxy.subprocess.Popen
            with patch.object(proxy.subprocess, "Popen", side_effect=lambda args, **kwargs:
                              popen([sys.executable, *args], **kwargs)):
                proxy.refresh_login(str(executable), Path(temp), timeout=3)


if __name__ == "__main__":
    unittest.main()
