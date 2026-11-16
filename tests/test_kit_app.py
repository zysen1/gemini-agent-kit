"""Offline HTTP fixtures for the local panel and gateway facade.

Only ephemeral loopback servers and temporary files are used. These tests never
download/start Antigravity Tools, import real accounts, contact Google, write a
real Codex configuration, or demonstrate a real model/client task.
"""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.version_info < (3, 11):
    raise unittest.SkipTest("The local GUI and Codex helper require Python 3.11+")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_desktop
import kit_app as app


MODEL = "gemini-offline-fixture"
UPSTREAM_KEY = "offline-manager-gateway-key-not-real"
ADMIN_KEY = "offline-manager-admin-key-not-real"
TOKEN = "1//offlineFixtureRefreshToken_not_a_real_account_1234567890"
EMAIL = "private-fixture@example.invalid"
FIRST_EVENT = b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\n'
LAST_EVENT = b'event: response.completed\ndata: {"response":{"status":"completed"}}\n\n'


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_bytes(self, status, raw, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, status, value, headers=None):
        self.send_bytes(status, json.dumps(value).encode(), headers)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path == "/v1/models":
            return self.send_json(200, {"data": [{"id": MODEL}, {"id": "gpt-other"},
                                                {"id": "gemini-<unsafe>"}, {"id": 123}]})
        if self.path == "/api/health":
            return self.send_json(200, {"status": "ok", "version": "5.0.3"})
        if self.path == "/api/proxy/status":
            return self.send_json(200, {"running": self.server.proxy_running})
        if self.path == "/api/accounts":
            if not self.server.accounts_present:
                return self.send_json(200, {"accounts": []})
            return self.send_json(200, {"accounts": [{"id": "fixture-account", "email": EMAIL,
                "refresh_token": TOKEN, "access_token": "fixture-access-token", "password": "fixture-password",
                "quota": {"subscription_tier": "PRO", "models": [{"name": MODEL, "percentage": 72},
                                                                   {"name": "other-model", "percentage": 99}]}}]})
        if self.path == "/api/accounts/fixture-account/quota":
            return self.send_json(200, {"quota": {"models": []}, "refresh_token": TOKEN})
        return self.send_json(404, {"error": "fixture-route-not-found"})

    def do_DELETE(self):
        self.server.calls.append(("DELETE", self.path, self.headers.get("Authorization"), None))
        self.send_json(200, {"removed": True, "refresh_token": TOKEN})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.loads(raw)
        self.server.calls.append(("POST", self.path, self.headers.get("Authorization"), payload))
        if self.path == "/api/accounts":
            if self.server.import_fails:
                return self.send_json(400, {"error": "fixture import rejected " + TOKEN})
            self.server.accounts_present = True
            return self.send_json(200, {"success": True, "refresh_token": TOKEN})
        if self.path == "/api/proxy/start":
            if self.server.proxy_start_fails:
                return self.send_json(503, {"error": "fixture start rejected " + UPSTREAM_KEY})
            self.server.proxy_running = True
            return self.send_json(200, {"running": True})
        if self.path not in ("/v1/responses", "/v1/chat/completions"):
            return self.send_json(404, {"error": "fixture-route-not-found"})
        if self.server.mode == "error":
            return self.send_json(429, {"error": {"message": UPSTREAM_KEY + " " + ADMIN_KEY + " " + TOKEN}},
                                  {"Retry-After": "4", "X-Mapped-Model": MODEL})
        if self.server.mode == "redirect":
            return self.send_json(302, {"redirect": "do not follow"}, {"Location": "http://127.0.0.1:1/never"})
        if self.server.mode == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.send_header("X-Mapped-Model", MODEL)
            self.send_header("X-Upstream-Model", MODEL)
            self.send_header("X-Private-Token", UPSTREAM_KEY)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(FIRST_EVENT)
            self.wfile.flush()
            self.server.first_sent.set()
            self.server.release_stream.wait(3)
            self.wfile.write(LAST_EVENT)
            self.wfile.flush()
            self.server.stream_completed.set()
            return
        if self.path == "/v1/chat/completions":
            prompt = payload["messages"][0]["content"]
            text = prompt.split("nothing else: ")[-1]
            if self.server.mode == "bad_nonce":
                text = "fixture reply that fails the expected nonce"
            elif self.server.mode == "success_cooldown_text":
                text = "A fictional instruction says retry after 99 s."
            return self.send_json(200, {"model": MODEL, "choices": [{"message": {"content": text}}]},
                                  {"X-Mapped-Model": MODEL, "X-Upstream-Model": MODEL})
        return self.send_json(200, {"id": "fixture-response", "model": MODEL, "output": [], "status": "completed"},
                              {"X-Mapped-Model": MODEL, "X-Upstream-Model": MODEL, "X-Private-Token": UPSTREAM_KEY})


class KitAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.fixture_home = self.root / "home"
        self.fixture_home.mkdir()
        self.home_patch = mock.patch.object(Path, "home", return_value=self.fixture_home)
        self.home_patch.start()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.daemon_threads = True
        self.upstream.calls = []
        self.upstream.mode = "normal"
        self.upstream.accounts_present = True
        self.upstream.proxy_running = True
        self.upstream.proxy_start_fails = False
        self.upstream.import_fails = False
        self.upstream.first_sent = threading.Event()
        self.upstream.release_stream = threading.Event()
        self.upstream.stream_completed = threading.Event()
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.upstream_thread.start()
        self.manager_config = self.root / "manager-fixture.json"
        self.manager_config.write_text(json.dumps({"proxy": {"port": self.upstream.server_port,
            "api_key": UPSTREAM_KEY, "admin_password": ADMIN_KEY}}))
        self.manager_config.chmod(0o600)
        self.core = app.KitCore(self.root / "private", port=8787, manager_config=self.manager_config, interval=0)
        self.server = app.make_server(self.core, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.static = self.root / "web"
        self.static.mkdir()
        for name in ("index.html", "app.js", "style.css"):
            (self.static / name).write_text("offline static fixture")
        self.web_patch = mock.patch.object(app, "WEB", self.static)
        self.web_patch.start()

    def tearDown(self):
        self.upstream.release_stream.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        self.web_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    @property
    def cookie(self):
        return "kit_session=" + self.server.session

    def headers(self, *, admin=False, gateway=False):
        headers = {"Content-Type": "application/json"}
        if admin:
            headers.update({"Cookie": self.cookie, "Origin": self.core.origin, "X-CSRF-Token": self.server.csrf})
        if gateway:
            headers["Authorization"] = "Bearer " + self.core.state["gateway_key"]
        return headers

    def request(self, path, *, method="GET", payload=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def admin(self, path, payload=None):
        return self.request(path, method="POST" if payload is not None else "GET", payload=payload, headers=self.headers(admin=True))

    def gateway(self, path, payload=None):
        return self.request(path, method="POST" if payload is not None else "GET", payload=payload, headers=self.headers(gateway=True))

    def posts(self):
        return [call for call in self.upstream.calls if call[0] == "POST"]

    def assert_no_private_values(self, raw):
        text = raw.decode() if isinstance(raw, bytes) else json.dumps(raw)
        for value in (UPSTREAM_KEY, ADMIN_KEY, TOKEN, self.core.state["gateway_key"], EMAIL,
                      "fixture-access-token", "fixture-password"):
            self.assertNotIn(value, text)

    def test_static_entry_sets_httponly_strict_cookie_and_csp(self):
        status, headers, raw = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(raw, b"offline static fixture")
        self.assertEqual(self.upstream.calls, [])

    def test_host_rebinding_is_rejected_before_any_account_or_gateway_access(self):
        for host in ("attacker.invalid", "localhost:" + str(self.server.server_port), "127.0.0.1:1"):
            with self.subTest(host=host):
                headers = self.headers(admin=True)
                headers["Host"] = host
                status, _, raw = self.request("/api/state", headers=headers)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(raw)["error"]["code"], "BAD_HOST")
        self.assertEqual(self.upstream.calls, [])

    def test_admin_requires_session_even_with_gateway_key(self):
        for path in ("/api/state", "/api/csrf"):
            status, _, _ = self.request(path, headers=self.headers(gateway=True))
            self.assertEqual(status, 401)
        self.assertEqual(self.upstream.calls, [])

    def test_gateway_requires_bearer_even_with_admin_cookie(self):
        for path in ("/v1/models", "/codex/models", "/v1/responses"):
            status, _, _ = self.request(path, headers=self.headers(admin=True))
            self.assertEqual(status, 401)
        self.assertEqual(self.upstream.calls, [])

    def test_mutation_requires_both_same_origin_and_csrf(self):
        for origin, token in ((None, self.server.csrf), ("https://attacker.invalid", self.server.csrf),
                              (self.core.origin, None), (self.core.origin, "wrong")):
            headers = {"Cookie": self.cookie, "Content-Type": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            if token is not None:
                headers["X-CSRF-Token"] = token
            status, _, raw = self.request("/api/accounts", method="POST", payload={"refresh_token": TOKEN}, headers=headers)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(raw)["error"]["code"], "CSRF")
        self.assertEqual(self.upstream.calls, [])

    def test_csrf_only_returned_in_authenticated_no_store_json(self):
        status, headers, raw = self.admin("/api/csrf")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"token": self.server.csrf})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_admin_query_credentials_and_form_posts_are_rejected(self):
        status, _, _ = self.admin("/api/state?token=fixture")
        self.assertEqual(status, 400)
        headers = self.headers(admin=True)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, _, raw = self.request("/api/accounts", method="POST", raw=b"refresh_token=fixture", headers=headers)
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(raw)["error"]["code"], "CONTENT_TYPE")
        self.assertEqual(self.upstream.calls, [])

    def test_bad_json_array_and_oversized_body_are_rejected(self):
        for raw in (b"not json", b"[]", b"null"):
            status, _, _ = self.request("/api/models/select", method="POST", raw=raw, headers=self.headers(admin=True))
            self.assertEqual(status, 400)
        headers = self.headers(admin=True)
        headers["Content-Length"] = str(app.MAX_BODY + 1)
        status, _, _ = self.request("/api/accounts", method="POST", raw=b"", headers=headers)
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream.calls, [])

    def test_transfer_encoded_admin_request_is_rejected(self):
        headers = self.headers(admin=True)
        headers["Transfer-Encoding"] = "chunked"
        status, _, raw = self.request("/api/accounts", method="POST", raw=b"0\r\n\r\n", headers=headers)
        self.assertEqual(status, 411)
        self.assertEqual(json.loads(raw)["error"]["code"], "BAD_LENGTH")

    def test_snapshot_redacts_accounts_and_uses_separate_upstream_credentials(self):
        status, _, raw = self.admin("/api/state")
        self.assertEqual(status, 200)
        self.assert_no_private_values(raw)
        state = json.loads(raw)
        self.assertEqual(state["accounts"][0]["label"], "p***@example.invalid")
        self.assertEqual(state["accounts"][0]["models"], [{"id": MODEL, "remaining_percent": 72}])
        self.assertFalse(state["codex"]["desktop_verified"])
        self.assertEqual(state["runtime"]["version"], "5.0.3")
        for _, path, auth, _ in self.upstream.calls:
            self.assertEqual(auth, "Bearer " + (ADMIN_KEY if path.startswith("/api/") else UPSTREAM_KEY))

    def test_models_and_native_codex_catalog_use_exact_filtered_ids(self):
        status, _, raw = self.gateway("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"], [{"id": MODEL}])
        status, _, raw = self.gateway("/codex/models?client_version=0.169.0")
        self.assertEqual(status, 200)
        catalog = json.loads(raw)
        model = catalog["models"][0]
        self.assertEqual(model["slug"], MODEL)
        self.assertEqual(model["visibility"], "list")
        self.assertEqual(model["supported_reasoning_levels"], [])
        self.assertTrue(model["base_instructions"])
        self.assertNotIn("tool_mode", model)
        self.assertNotIn("guardian", model)
        self.assert_no_private_values(raw)
        self.assertEqual(self.posts(), [])

    def test_unknown_model_cannot_be_silently_substituted(self):
        status, _, _ = self.gateway("/v1/responses", {"model": "gemini-unavailable", "input": "fixture"})
        self.assertEqual(status, 400)
        self.assertEqual(self.posts(), [])

    def test_gateway_posts_preserve_payload_and_replace_kit_credential(self):
        payload = {"model": MODEL, "input": "fixture input", "tools": [{"type": "function", "name": "fixture_tool"}],
                   "extra": {"must": "remain identical"}, "stream": False}
        status, headers, raw = self.gateway("/v1/responses", payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.posts(), [("POST", "/v1/responses", "Bearer " + UPSTREAM_KEY, payload)])
        self.assertEqual(json.loads(raw)["model"], MODEL)
        self.assertEqual(headers["X-Mapped-Model"], MODEL)
        self.assertNotIn("X-Private-Token", headers)
        self.assertNotIn("Set-Cookie", headers)

    def test_successful_sse_is_forwarded_incrementally_and_without_retries(self):
        self.upstream.mode = "sse"
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            conn.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": "fixture", "stream": True}), self.headers(gateway=True))
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            self.assertEqual(response.read(len(FIRST_EVENT)), FIRST_EVENT)
            self.assertTrue(self.upstream.first_sent.is_set())
            self.assertFalse(self.upstream.stream_completed.is_set(), "first SSE event must reach client before upstream completes")
            self.upstream.release_stream.set()
            self.assertEqual(response.read(), LAST_EVENT)
        finally:
            self.upstream.release_stream.set()
            conn.close()
        self.assertEqual(len(self.posts()), 1)
        log = (self.core.data_dir / "calls.jsonl").read_text()
        self.assertNotIn("hello", log)
        self.assertNotIn("fixture input", log)
        self.assert_no_private_values(log)
        self.assertEqual(json.loads(log)["automatic_retries"], 0)

    def test_upstream_http_error_does_not_expose_manager_secrets_or_retry(self):
        self.upstream.mode = "error"
        status, headers, raw = self.gateway("/v1/responses", {"model": MODEL, "input": "fixture"})
        self.assertEqual(status, 429)
        self.assertGreaterEqual(float(headers["Retry-After"]), 4)
        self.assert_no_private_values(raw)
        self.assertEqual(len(self.posts()), 1)
        self.assert_no_private_values((self.core.data_dir / "calls.jsonl").read_text())

    def test_upstream_redirect_not_followed_and_location_not_exposed(self):
        self.upstream.mode = "redirect"
        status, headers, _ = self.gateway("/v1/responses", {"model": MODEL, "input": "fixture"})
        self.assertTrue(300 <= status < 600)
        self.assertNotIn("Location", headers)
        self.assertEqual(len(self.posts()), 1)

    def test_recorded_model_headers_are_sanitized_and_case_insensitive(self):
        self.core.record_call(MODEL, 200, 0.1, {"x-mapped-model": MODEL, "X-Upstream-Model": UPSTREAM_KEY})
        raw = (self.core.data_dir / "calls.jsonl").read_text()
        record = json.loads(raw)
        self.assertEqual(record["mapped_model"], MODEL)
        self.assertIsNone(record["upstream_model"])
        self.assert_no_private_values(raw)

    def test_check_records_only_metadata_and_requires_correct_nonce(self):
        status, _, raw = self.admin("/api/check", {"model": MODEL})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["passed"])
        self.assertEqual(len(self.posts()), 1)
        self.assert_no_private_values(raw)
        record = (self.core.data_dir / "calls.jsonl").read_text()
        prompt = self.posts()[0][3]["messages"][0]["content"]
        self.assertNotIn(prompt, record)
        self.assertNotIn("kit-" + prompt.split("kit-")[-1], record)
        self.upstream.mode = "bad_nonce"
        status, _, raw = self.admin("/api/check", {"model": MODEL})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["passed"])
        self.assertEqual(len(self.posts()), 2)

    def test_successful_content_cannot_inject_a_pacing_cooldown(self):
        self.upstream.mode = "success_cooldown_text"
        with mock.patch.object(self.core.gate, "finish", wraps=self.core.gate.finish) as finish:
            status, _, _ = self.gateway("/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": "fixture"}]})
        self.assertEqual(status, 200)
        finish.assert_called_once_with(0)

    def test_import_goes_directly_to_manager_and_is_not_retained_or_returned(self):
        status, _, raw = self.admin("/api/accounts", {"refresh_token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(self.posts(), [("POST", "/api/accounts", "Bearer " + ADMIN_KEY, {"refreshToken": TOKEN}),
                                       ("POST", "/api/proxy/start", "Bearer " + ADMIN_KEY, {})])
        self.assert_no_private_values(raw)
        self.assertNotIn(TOKEN, self.core.state_path.read_text())
        self.assertFalse((self.core.data_dir / "calls.jsonl").exists())

    def test_empty_manager_remains_available_for_import_then_enables_models(self):
        self.upstream.accounts_present = False
        self.upstream.proxy_running = False
        status, _, raw = self.admin("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state["runtime"]["status"], "running")
        self.assertFalse(state["runtime"]["proxy_running"])
        self.assertEqual(state["runtime"]["version"], "5.0.3")
        self.assertEqual(state["accounts"], [])
        self.assertEqual(state["models"], [])
        self.assertEqual(self.posts(), [])
        self.assertNotIn("/v1/models", [call[1] for call in self.upstream.calls])
        status, _, raw = self.admin("/api/accounts", {"refresh_token": TOKEN})
        self.assertEqual(status, 200)
        self.assert_no_private_values(raw)
        self.assertEqual([call[1] for call in self.posts()], ["/api/accounts", "/api/proxy/start"])
        self.assertTrue(self.upstream.accounts_present)
        self.assertTrue(self.upstream.proxy_running)
        status, _, raw = self.admin("/api/state")
        state = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(state["runtime"]["proxy_running"])
        self.assertEqual(len(state["accounts"]), 1)
        self.assertEqual(state["models"], [{"id": MODEL}])
        self.assertIsNone(state["notice"])
        self.assertEqual(len(self.posts()), 2, "snapshot must not generate or restart the proxy")

    def test_import_success_proxy_start_failure_does_not_repeat_import(self):
        self.upstream.accounts_present = False
        self.upstream.proxy_running = False
        self.upstream.proxy_start_fails = True
        status, _, raw = self.admin("/api/accounts", {"refresh_token": TOKEN})
        result = json.loads(raw)
        self.assertEqual(status, 200, "an imported account must not be reported as a failed import")
        self.assertTrue(result["ok"])
        self.assertIn("账号已导入", result["message"])
        self.assertIn("启动", result["message"])
        self.assert_no_private_values(raw)
        self.assertTrue(self.upstream.accounts_present)
        self.assertFalse(self.upstream.proxy_running)
        _, _, raw = self.admin("/api/state")
        state = json.loads(raw)
        self.assertEqual(state["runtime"]["status"], "running")
        self.assertFalse(state["runtime"]["proxy_running"])
        self.assertEqual(len(state["accounts"]), 1)
        self.assertEqual(state["models"], [])
        self.assertIn("启动", state["notice"])
        self.upstream.proxy_start_fails = False
        status, _, raw = self.admin("/api/runtime/start", {})
        self.assertEqual(status, 200)
        self.assert_no_private_values(raw)
        self.assertTrue(self.upstream.proxy_running)
        self.assertEqual([call[1] for call in self.posts()], ["/api/accounts", "/api/proxy/start", "/api/proxy/start"])
        self.assertNotIn(TOKEN, self.core.state_path.read_text())

    def test_rejected_import_does_not_try_starting_the_proxy(self):
        self.upstream.accounts_present = False
        self.upstream.proxy_running = False
        self.upstream.import_fails = True
        status, _, raw = self.admin("/api/accounts", {"refresh_token": TOKEN})
        self.assertEqual(status, 502)
        self.assert_no_private_values(raw)
        self.assertEqual([call[1] for call in self.posts()], ["/api/accounts"])
        self.assertFalse(self.upstream.accounts_present)
        self.assertFalse(self.upstream.proxy_running)

    def test_owned_runtime_starts_management_without_enabling_empty_proxy(self):
        self.core.manager_config = None
        self.upstream.accounts_present = False
        self.upstream.proxy_running = False
        origin = "http://127.0.0.1:" + str(self.upstream.server_port)
        with mock.patch.object(self.core, "upstream", return_value=(origin, UPSTREAM_KEY, ADMIN_KEY)), \
                mock.patch.object(self.core.runtime, "start", return_value={"status": "running"}) as start:
            status, _, raw = self.admin("/api/runtime/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "running")
        start.assert_called_once_with()
        self.assertEqual(self.upstream.calls, [("GET", "/api/accounts", "Bearer " + ADMIN_KEY, None)])
        self.assertFalse(self.upstream.proxy_running)

    def test_owned_runtime_with_existing_account_enables_proxy_once(self):
        self.core.manager_config = None
        self.upstream.proxy_running = False
        origin = "http://127.0.0.1:" + str(self.upstream.server_port)
        with mock.patch.object(self.core, "upstream", return_value=(origin, UPSTREAM_KEY, ADMIN_KEY)), \
                mock.patch.object(self.core.runtime, "start", return_value={"status": "running"}) as start:
            status, _, _ = self.admin("/api/runtime/start", {})
        self.assertEqual(status, 200)
        start.assert_called_once_with()
        self.assertEqual(self.posts(), [("POST", "/api/proxy/start", "Bearer " + ADMIN_KEY, {})])
        self.assertTrue(self.upstream.proxy_running)

    def test_invalid_import_and_path_injection_never_reach_manager(self):
        for token in ("not a token", "email|password|1//token", TOKEN + "\nextra", None):
            status, _, _ = self.admin("/api/accounts", {"refresh_token": token})
            self.assertEqual(status, 400)
        for ident in ("../other", "one/two", "a?query=1", None):
            status, _, _ = self.admin("/api/accounts/remove", {"id": ident})
            self.assertEqual(status, 400)
        self.assertEqual(self.upstream.calls, [])

    def test_account_remove_returns_no_upstream_private_payload(self):
        status, _, raw = self.admin("/api/accounts/remove", {"id": "fixture-account"})
        self.assertEqual(status, 200)
        self.assert_no_private_values(raw)
        self.assertEqual(self.upstream.calls, [("DELETE", "/api/accounts/fixture-account", "Bearer " + ADMIN_KEY, None)])

    def test_selected_model_persists_and_does_not_generate(self):
        status, _, raw = self.admin("/api/models/select", {"model": MODEL})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["model"], MODEL)
        self.assertEqual(json.loads(self.core.state_path.read_text())["selected_model"], MODEL)
        self.assertEqual(self.posts(), [])

    def test_helper_uses_private_kit_key_and_provider_state_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = codex_desktop.main(["credential", "--state", str(self.core.state_path)])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), self.core.state["gateway_key"])
        self.assertNotIn(UPSTREAM_KEY, output.getvalue())
        self.assertNotIn(ADMIN_KEY, output.getvalue())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.core.state_path.stat().st_mode), 0o600)
        # Fixture-only consumption; secret stdout is deliberately not printed.

    def test_codex_preview_does_not_apply_or_claim_desktop_validation(self):
        fixture = {"before_sha256": "a" * 64, "desktop_verified": False, "diff": "fixture diff"}
        with mock.patch.object(codex_desktop, "preview", return_value=fixture) as preview, mock.patch.object(codex_desktop, "apply") as apply:
            status, _, raw = self.admin("/api/codex/preview", {"model": MODEL})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["preview"]["desktop_verified"])
        self.assertFalse(preview.call_args.kwargs["activate"])
        self.assertEqual(preview.call_args.kwargs["state_path"], self.core.state_path)
        self.assertEqual(preview.call_args.kwargs["base_url"], self.core.origin + "/v1")
        apply.assert_not_called()

    def test_codex_apply_preserves_previous_receipt_on_idempotent_result(self):
        self.core.state["last_codex_receipt"] = "/fixture/private/previous.receipt.json"
        plan = {"before_sha256": "a" * 64, "desktop_verified": False, "diff": "fixture diff"}
        with mock.patch.object(codex_desktop, "preview", return_value=plan):
            self.admin("/api/codex/preview", {"model": MODEL})
        with mock.patch.object(codex_desktop, "apply", return_value={"changed": False, "receipt_path": None, "desktop_verified": False}):
            status, _, _ = self.admin("/api/codex/apply", {"model": MODEL, "expected_before_sha256": "a" * 64})
        self.assertEqual(status, 200)
        self.assertEqual(self.core.state["last_codex_receipt"], "/fixture/private/previous.receipt.json")

    def test_codex_apply_forwards_reviewed_hash_and_never_activates_by_request(self):
        expected = "b" * 64
        plan = {"before_sha256": expected, "desktop_verified": False, "diff": "fixture diff"}
        with mock.patch.object(codex_desktop, "preview", return_value=plan):
            self.admin("/api/codex/preview", {"model": MODEL})
        with mock.patch.object(codex_desktop, "apply", return_value={"changed": True, "receipt_path": "/fixture/new.receipt.json", "desktop_verified": False}) as apply:
            status, _, _ = self.admin("/api/codex/apply", {"model": MODEL, "activate": True, "expected_before_sha256": expected})
        self.assertEqual(status, 200)
        self.assertEqual(apply.call_args.kwargs["expected_before_sha256"], expected)
        self.assertFalse(apply.call_args.kwargs["activate"])
        self.assertEqual(self.core.state["last_codex_receipt"], "/fixture/new.receipt.json")

    def test_codex_apply_requires_review_hash_and_accepts_reviewed_absent_file(self):
        with mock.patch.object(codex_desktop, "apply", return_value={"changed": False, "receipt_path": None}) as apply:
            status, _, raw = self.admin("/api/codex/apply", {"model": MODEL})
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(raw)["error"]["code"], "PREVIEW_REQUIRED")
            apply.assert_not_called()
            status, _, _ = self.admin("/api/codex/apply", {"model": MODEL, "expected_before_sha256": None})
            self.assertEqual(status, 200)
            self.assertIsNone(apply.call_args.kwargs["expected_before_sha256"])

    def test_codex_restore_uses_its_own_receipt_and_ignores_client_path(self):
        receipt = "/fixture/private/owned.receipt.json"
        self.core.state["last_codex_receipt"] = receipt
        with mock.patch.object(codex_desktop, "restore", return_value={"restored": True}) as restore:
            status, _, _ = self.admin("/api/codex/restore", {"receipt_path": "/unrelated/private/file"})
        self.assertEqual(status, 200)
        restore.assert_called_once_with(self.fixture_home / ".codex" / "config.toml", receipt)
        self.assertIsNone(self.core.state["last_codex_receipt"])

    def test_externally_managed_service_cannot_be_stopped_by_panel(self):
        status, _, raw = self.admin("/api/runtime/stop", {})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "EXTERNAL_SERVICE")
        self.assertEqual(self.upstream.calls, [])

    def test_unhandled_local_error_returns_generic_text_without_secret(self):
        with mock.patch.object(self.core, "snapshot", side_effect=ValueError(UPSTREAM_KEY + TOKEN)):
            status, _, raw = self.admin("/api/state")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(raw)["error"]["code"], "LOCAL_ERROR")
        self.assert_no_private_values(raw)

    def test_only_explicit_loopback_origins_are_accepted(self):
        for origin in ("https://127.0.0.1:8787", "http://localhost:8787", "http://127.0.0.1:8787/v1",
                       "http://user:pass@127.0.0.1:8787", "http://127.0.0.1:8787?secret=x", "http://example.invalid:80"):
            with self.subTest(origin=origin):
                with self.assertRaises(app.AppError):
                    app.local_origin(origin)


if __name__ == "__main__":
    unittest.main()
