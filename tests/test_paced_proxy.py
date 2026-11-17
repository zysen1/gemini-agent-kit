"""Loopback fixtures only; these tests never call a real model or account."""
import concurrent.futures
from email.utils import formatdate
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.parse


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paced_proxy.py"
spec = importlib.util.spec_from_file_location("paced_proxy_under_test", SCRIPT)
proxy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proxy
spec.loader.exec_module(proxy)

FIXTURE_KEY = "fixture-secret-not-a-real-gateway-key"
PRIVATE_PROMPT = "fixture-private-prompt-must-not-appear-in-metadata"
REQUEST_BODY = json.dumps({"model": "fixture/argon", "messages": [{"role": "user", "content": PRIVATE_PROMPT}]}).encode()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


class ObservableGate(proxy.PacingGate):
    def __init__(self, clock):
        super().__init__(interval=35, clock=clock.monotonic, sleep=clock.sleep)
        self.begin_count = 0
        self.begin_lock = threading.Lock()
        self.second_entered = threading.Event()

    def begin(self, cancelled=None):
        with self.begin_lock:
            self.begin_count += 1
            if self.begin_count == 2:
                self.second_entered.set()
        return super().begin(cancelled=cancelled)


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self, status, raw, headers=None):
        try:
            self.send_response(status, self.server.error_reason if status >= 400 else self.server.success_reason)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        with self.server.state_lock:
            self.server.calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        if self.server.mode == "get_error":
            self.respond(403, self.server.error_body, self.server.error_headers)
        elif urllib.parse.urlsplit(self.path).path == "/v1/models":
            self.respond(200, b'{"data":[{"id":"fixture/argon"}]}')
        elif self.path == "/v1/version":
            self.respond(200, b'{"version":"fixture-only"}')
        else:
            self.respond(404, b'{"error":"fixture unknown route"}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.state_lock:
            self.server.calls.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": raw})
            self.server.active += 1
            self.server.max_active = max(self.server.max_active, self.server.active)
            post_count = sum(call["method"] == "POST" for call in self.server.calls)
        mode = self.server.mode
        try:
            if mode == "hold" and post_count == 1:
                self.server.first_started.set()
                self.server.release.wait(4)
            if mode == "sse":
                self.send_response(200, self.server.success_reason)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("X-Model-ID", "gemini-fixture")
                for key, value in self.server.success_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                first = b'data: {"delta":"first"}\n\n'
                self.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
                self.wfile.flush()
                self.server.first_frame_sent.set()
                self.server.release.wait(4)
                last = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(last):x}\r\n".encode() + last + b"\r\n0\r\n\r\n")
                self.wfile.flush()
                self.server.stream_finished.set()
                return
            if mode == "429":
                return self.respond(429, self.server.error_body, {
                    "Retry-After": "61", "X-Model-ID": "fixture/argon", **self.server.error_headers})
            if mode == "503":
                return self.respond(503, self.server.error_body, self.server.error_headers)
            if mode.startswith("error:"):
                return self.respond(int(mode.split(":")[1]), self.server.error_body, self.server.error_headers)
            if mode == "redirect":
                return self.respond(307, b"redirect body", {"Location": f"http://127.0.0.1:{self.server.server_port}/redirect-target"})
            if mode == "truncated":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"partial":true}')
                self.wfile.flush()
                self.close_connection = True
                return
            self.respond(200, b'{"model":"fixture/argon","ok":true}', {
                "X-Model-ID": "gemini-fixture", "X-Actual-Model": "gemini-fixture-route",
                "Connection": "X-Remove-Response", "X-Remove-Response": "hop-only",
                "Keep-Alive": "timeout=5", **self.server.success_headers,
            })
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.state_lock:
                self.server.active -= 1


class TestPacedProxy(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = ObservableGate(self.clock)
        self.metadata = []
        self.metadata_event = threading.Event()

        def record(event):
            self.metadata.append(event)
            self.metadata_event.set()

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.upstream.daemon_threads = True
        self.upstream.mode = "ok"
        self.upstream.state_lock = threading.Lock()
        self.upstream.calls = []
        self.upstream.active = 0
        self.upstream.max_active = 0
        self.upstream.release = threading.Event()
        self.upstream.first_started = threading.Event()
        self.upstream.first_frame_sent = threading.Event()
        self.upstream.stream_finished = threading.Event()
        self.upstream.error_body = b'{"error":{"message":"All accounts rate-limited. Cooldown wait 47s."}}'
        self.upstream.error_headers = {}
        self.upstream.error_reason = None
        self.upstream.success_headers = {}
        self.upstream.success_reason = None
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        config = proxy.ProxyConfig(key=FIXTURE_KEY, upstream_port=self.upstream.server_port,
                                   interval=35, gate=self.gate, metadata_log=record, upstream_timeout=5)
        self.server = proxy.make_server(config, port=0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.upstream.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()

    def request(self, method="POST", path="/v1/responses", *, headers=None, body=REQUEST_BODY):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        req_headers = {"Authorization": "Bearer " + FIXTURE_KEY, "Content-Type": "application/json"}
        req_headers.update(headers or {})
        try:
            connection.request(method, path, body=body if method == "POST" else None, headers=req_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_loopback_binding_and_authorized_passthrough_without_logging_payload(self):
        status, headers, raw = self.request()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["model"], "fixture/argon")
        self.assertEqual(headers.get("X-Model-ID"), "gemini-fixture")
        self.assertEqual(headers.get("X-Actual-Model"), "gemini-fixture-route")
        self.assertEqual(self.upstream.calls[0]["headers"]["Authorization"], "Bearer " + FIXTURE_KEY)
        self.assertEqual(self.upstream.calls[0]["body"], REQUEST_BODY)
        self.assertTrue(self.metadata_event.wait(1))
        encoded = json.dumps(self.metadata)
        self.assertNotIn(FIXTURE_KEY, encoded)
        self.assertNotIn(PRIVATE_PROMPT, encoded)
        self.assertNotIn("Authorization", encoded)

    def test_auth_unknown_route_and_absolute_remote_target_never_reach_upstream(self):
        for method, path, headers in (
            ("POST", "/v1/responses", {"Authorization": "Bearer wrong"}),
            ("POST", "/v1/unknown", {}),
            ("POST", "http://example.com/v1/responses", {}),
            ("GET", "/not-allowed", {}),
        ):
            with self.subTest(path=path):
                status, _, _ = self.request(method, path, headers=headers)
                self.assertGreaterEqual(status, 400)
                self.assertLess(status, 500)
        self.assertFalse(self.upstream.calls)

    def test_models_allows_only_one_client_version_and_metadata_omits_query(self):
        path = "/v1/models?client_version=0.139.0"
        status, _, raw = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"][0]["id"], "fixture/argon")
        self.assertEqual(self.upstream.calls[0]["path"], path)
        self.assertFalse(self.clock.sleeps)
        self.assertTrue(self.metadata_event.wait(1))
        self.assertEqual(self.metadata[-1]["route"], "/v1/models")
        self.assertNotIn("0.139.0", json.dumps(self.metadata))
        invalid = [
            ("GET", "/v1/models?client_version="),
            ("GET", "/v1/models?unknown=1"),
            ("GET", "/v1/models?client_version=1&client_version=2"),
            ("GET", "/v1/models?client_version=1&unknown=2"),
            ("GET", "/v1/version?client_version=1"),
            ("GET", "/readyz?client_version=1"),
            ("POST", "/v1/responses?client_version=1"),
        ]
        for method, target in invalid:
            with self.subTest(target=target):
                self.assertEqual(self.request(method, target)[0], 404)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_hop_by_hop_and_connection_named_headers_removed(self):
        status, headers, _ = self.request(headers={"Connection": "X-Remove-Request", "X-Remove-Request": "hop-only", "TE": "trailers"})
        self.assertEqual(status, 200)
        upstream_headers = {key.lower(): value for key, value in self.upstream.calls[0]["headers"].items()}
        self.assertNotIn("x-remove-request", upstream_headers)
        self.assertNotIn("te", upstream_headers)
        response_headers = {key.lower(): value for key, value in headers.items()}
        self.assertNotIn("x-remove-response", response_headers)
        self.assertNotIn("keep-alive", response_headers)

    def test_chunked_request_and_oversized_length_are_rejected_before_upstream(self):
        for extra in ({"Transfer-Encoding": "chunked"}, {"Content-Length": str(32 * 1024 * 1024 + 1)}):
            with self.subTest(headers=extra):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
                try:
                    connection.putrequest("POST", "/v1/responses")
                    connection.putheader("Authorization", "Bearer " + FIXTURE_KEY)
                    for key, value in extra.items():
                        connection.putheader(key, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertGreaterEqual(response.status, 400)
                    self.assertLess(response.status, 500)
                    response.read()
                finally:
                    connection.close()
        self.assertFalse(self.upstream.calls)

    def test_sse_first_frame_arrives_before_upstream_finishes(self):
        self.upstream.mode = "sse"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("POST", "/v1/chat/completions", REQUEST_BODY, {"Authorization": "Bearer " + FIXTURE_KEY, "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(self.upstream.first_frame_sent.wait(1))
            self.assertEqual(response.readline(), b'data: {"delta":"first"}\n')
            self.assertFalse(self.upstream.stream_finished.is_set())
            self.upstream.release.set()
            self.assertIn(b"data: [DONE]", response.read())
            self.assertEqual(response.getheader("X-Model-ID"), "gemini-fixture")
        finally:
            self.upstream.release.set()
            connection.close()

    def test_successful_json_and_sse_strip_sensitive_response_headers_and_reason(self):
        secret = "fixture-sensitive-success-header"
        self.upstream.success_headers = {
            "Set-Cookie": "session=" + secret, "Authorization": "Bearer " + secret,
            "X-Private-Token": secret, "WWW-Authenticate": secret,
            "X-Mapped-Model": secret + " invalid model", "Cache-Control": "private; " + secret,
        }
        self.upstream.success_reason = secret
        for mode in ("ok", "sse"):
            with self.subTest(mode=mode):
                self.upstream.mode = mode
                self.upstream.release.set()
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
                try:
                    connection.request("POST", "/v1/responses", REQUEST_BODY, {"Authorization": "Bearer " + FIXTURE_KEY})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.reason, "OK")
                    self.assertNotIn(secret, str(response.getheaders()))
                    self.assertEqual(response.getheader("X-Model-ID"), "gemini-fixture")
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    raw = response.read()
                    self.assertIn(b"data: [DONE]" if mode == "sse" else b'"ok":true', raw)
                finally:
                    connection.close()

    def test_response_header_allowlist_preserves_encoding_and_safe_model_only(self):
        headers = proxy.safe_response_headers([
            ("Content-Type", "application/json; charset=UTF-8; token=fixture-private"),
            ("Content-Encoding", "gzip"), ("X-Upstream-Model", "models/gemini-fixture"),
            ("X-Actual-Model", "Bearer fixture-private"), ("X-Private-Token", "fixture-private"),
        ])
        self.assertEqual(headers, [("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Encoding", "gzip"), ("X-Upstream-Model", "models/gemini-fixture")])

    def test_client_cookies_and_proxy_credentials_are_not_sent_to_upstream(self):
        status, _, _ = self.request(headers={
            "Cookie": "unrelated=fixture-private", "Cookie2": "fixture-private",
            "Proxy-Authorization": "Basic fixture-private", "Anthropic-Version": "2023-06-01",
        })
        self.assertEqual(status, 200)
        headers = {key.lower(): value for key, value in self.upstream.calls[0]["headers"].items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("cookie2", headers)
        self.assertNotIn("proxy-authorization", headers)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_429_status_preserved_body_sanitized_and_retry_after_delays_next_request(self):
        self.upstream.mode = "429"
        status, headers, raw = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(raw)["code"], "upstream_error")
        self.assertNotIn(b"All accounts", raw)
        self.assertNotIn("X-Model-ID", headers)
        self.assertEqual(headers.get("Retry-After"), "61")
        self.assertEqual(len(self.upstream.calls), 1, "The proxy must not automatically retry")
        self.upstream.mode = "ok"
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.upstream.calls), 2)
        self.assertEqual(sum(self.clock.sleeps), 61)

    def test_503_body_sanitized_and_manager_cooldown_used_without_retry(self):
        self.upstream.mode = "503"
        status, headers, raw = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)["code"], "upstream_error")
        self.assertNotIn(b"All accounts", raw)
        self.assertEqual(headers.get("Retry-After"), "47")
        self.assertEqual(len(self.upstream.calls), 1)
        self.upstream.mode = "ok"
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(sum(self.clock.sleeps), 47)

    def test_upstream_errors_never_disclose_diagnostic_bodies_or_headers(self):
        secret = "fixture-sensitive-diagnostic"
        self.upstream.error_body = json.dumps({"error": {"message": secret,
            "refresh_token": "1//" + secret, "email": "fixture@example.invalid", "retryDelay": "4.25s"}}).encode()
        self.upstream.error_reason = secret
        self.upstream.error_headers = {
            "Set-Cookie": "session=" + secret, "Authorization": "Bearer " + secret,
            "X-API-Key": secret, "X-Debug-Account": "fixture@example.invalid",
            "WWW-Authenticate": secret, "Retry-After": "2", "X-Model-ID": secret,
        }
        expected_body = {"code": "upstream_error",
                         "message": "Upstream request did not complete. Check account access or quota."}
        for status in (400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.upstream.mode = "error:" + str(status)
                actual_status, headers, raw = self.request()
                self.assertEqual(actual_status, status)
                self.assertEqual(json.loads(raw), expected_body)
                self.assertNotIn(secret, json.dumps(headers) + raw.decode())
                self.assertNotIn("fixture@example.invalid", json.dumps(headers) + raw.decode())
                self.assertEqual(headers.get("Retry-After"), "5" if status in (429, 503) else None)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.upstream.calls), 7, "Each request has exactly one upstream attempt")
        self.assertTrue(self.metadata_event.wait(1))
        self.assertNotIn(secret, json.dumps(self.metadata))
        self.upstream.mode = "get_error"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("GET", "/v1/models", headers={"Authorization": "Bearer " + FIXTURE_KEY})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertEqual(response.reason, "Forbidden")
            self.assertEqual(json.loads(response.read()), expected_body)
            self.assertNotIn(secret, str(response.getheaders()))
        finally:
            connection.close()

    def test_partial_request_body_times_out_without_an_upstream_call(self):
        self.server.proxy_config.request_timeout = 0.05
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.putrequest("POST", "/v1/responses")
            connection.putheader("Authorization", "Bearer " + FIXTURE_KEY)
            connection.putheader("Content-Length", "100")
            connection.endheaders()
            connection.send(b'{"partial":')
            response = connection.getresponse()
            self.assertEqual(response.status, 408)
            self.assertEqual(json.loads(response.read())["code"], "request_timeout")
        finally:
            connection.close()
        self.assertFalse(self.upstream.calls)
        self.assertEqual(self.gate.begin_count, 0)
        self.assertEqual(self.gate.next_allowed, 0)

    def test_all_post_routes_share_completion_based_35_second_gate(self):
        for path in ("/v1/responses", "/v1/chat/completions", "/v1/messages"):
            self.assertEqual(self.request(path=path)[0], 200)
        self.assertEqual(sum(self.clock.sleeps), 70)
        self.assertEqual(len(self.upstream.calls), 3)

    def test_gate_interval_starts_after_request_completion(self):
        clock = FakeClock()
        gate = proxy.PacingGate(interval=35, clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(gate.begin(), 0)
        clock.sleep(19)  # Time spent inside the upstream request, not queue wait.
        gate.finish()
        self.assertEqual(gate.begin(), 35)
        gate.finish(cooldown=72)
        self.assertEqual(gate.begin(), 72)
        gate.finish()
        self.assertEqual(clock.sleeps, [19, 35, 72])

    def test_inflight_posts_are_serial_but_get_models_is_not_queued(self):
        self.upstream.mode = "hold"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request)
            self.assertTrue(self.upstream.first_started.wait(1))
            second = pool.submit(self.request)
            try:
                self.assertTrue(self.gate.second_entered.wait(1))
                status, _, raw = self.request("GET", "/v1/models")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["data"][0]["id"], "fixture/argon")
                self.assertEqual(sum(call["method"] == "POST" for call in self.upstream.calls), 1)
            finally:
                self.upstream.release.set()
            self.assertEqual(first.result(timeout=2)[0], 200)
            self.assertEqual(second.result(timeout=2)[0], 200)
        self.assertEqual(self.upstream.max_active, 1)
        self.assertEqual(sum(self.clock.sleeps), 35)

    def test_disconnected_client_waiting_for_inflight_request_is_not_forwarded(self):
        self.upstream.mode = "hold"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.request)
            self.assertTrue(self.upstream.first_started.wait(1))
            second = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            try:
                second.request("POST", "/v1/responses", REQUEST_BODY,
                               {"Authorization": "Bearer " + FIXTURE_KEY})
                self.assertTrue(self.gate.second_entered.wait(1))
                second.close()
                self.assertTrue(self.metadata_event.wait(1), "A cancelled request must leave the queue promptly")
                cancelled = self.metadata[-1]
                self.assertEqual(cancelled["error_code"], "client_disconnected_before_upstream")
                self.assertFalse(cancelled["upstream_attempted"])
                self.assertIsNone(cancelled["status"])
                self.assertIsNone(cancelled["upstream_status"])
                self.assertIsNone(cancelled["next_minimum_wait_seconds"])
                self.assertEqual(len(self.upstream.calls), 1)
                self.assertEqual(self.gate.next_allowed, 0)
            finally:
                second.close()
                self.upstream.release.set()
            self.assertEqual(first.result(timeout=2)[0], 200)
        # Cancellation must neither extend nor bypass the first request's wait.
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(sum(self.clock.sleeps), 35)
        self.assertEqual(len(self.upstream.calls), 2)

    def test_disconnected_client_during_cooldown_preserves_existing_deadline(self):
        entered_sleep = threading.Event()
        release_sleep = threading.Event()
        original_sleep = self.clock.sleep

        def blocked_fake_sleep(seconds):
            entered_sleep.set()
            release_sleep.wait(2)
            original_sleep(seconds)

        self.gate.sleep = blocked_fake_sleep
        self.gate.next_allowed = self.clock.monotonic() + 35
        deadline = self.gate.next_allowed
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            client.request("POST", "/v1/responses", REQUEST_BODY,
                           {"Authorization": "Bearer " + FIXTURE_KEY})
            self.assertTrue(entered_sleep.wait(1))
            client.close()
            release_sleep.set()
            self.assertTrue(self.metadata_event.wait(1))
            self.assertEqual(self.metadata[-1]["error_code"], "client_disconnected_before_upstream")
            self.assertFalse(self.metadata[-1]["upstream_attempted"])
            self.assertFalse(self.upstream.calls)
            self.assertEqual(self.gate.next_allowed, deadline)
            self.assertFalse(self.gate.lock.locked())
        finally:
            release_sleep.set()
            client.close()
        self.gate.sleep = original_sleep
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(sum(self.clock.sleeps), 35)

    def test_cancelled_gate_before_lock_and_after_acquire_does_not_change_deadline(self):
        gate = proxy.PacingGate(interval=35, clock=self.clock.monotonic, sleep=self.clock.sleep)
        gate.next_allowed = 1100
        gate.lock.acquire()
        try:
            with self.assertRaises(proxy.ClientDisconnected):
                gate.begin(cancelled=lambda: True)
            self.assertTrue(gate.lock.locked(), "The other request's lock must remain held")
        finally:
            gate.lock.release()
        checks = iter([False, True])
        with self.assertRaises(proxy.ClientDisconnected):
            gate.begin(cancelled=lambda: next(checks))
        self.assertFalse(gate.lock.locked())
        self.assertEqual(gate.next_allowed, 1100)
        self.assertEqual(self.clock.sleeps, [])

    def test_disconnect_at_last_forward_check_releases_without_new_cooldown(self):
        # The first two checks admit/acquire the gate; disconnect just before
        # connection.request must still prevent the upstream call.
        with mock.patch.object(proxy.ProxyHandler, "client_disconnected", side_effect=[False, False, True]):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request()
        self.assertTrue(self.metadata_event.wait(1))
        self.assertFalse(self.upstream.calls)
        self.assertFalse(self.gate.lock.locked())
        self.assertEqual(self.gate.next_allowed, 0)
        self.assertEqual(self.metadata[-1]["error_code"], "client_disconnected_before_upstream")
        self.assertFalse(self.metadata[-1]["upstream_attempted"])
        self.assertIsNone(self.metadata[-1]["status"])

    def test_get_version_and_readiness_do_not_advance_gate(self):
        for route in ("/v1/version", "/healthz", "/readyz"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", route)[0], 200)
        self.assertEqual(self.gate.begin_count, 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_upstream_redirect_blocked_without_location_or_second_request(self):
        self.upstream.mode = "redirect"
        status, headers, raw = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.assertIn(b"upstream_redirect_blocked", raw)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_truncated_content_length_does_not_fake_completed_stream(self):
        self.upstream.mode = "truncated"
        with self.assertRaises(http.client.IncompleteRead):
            self.request()
        self.assertTrue(self.metadata_event.wait(1))
        self.assertEqual(self.metadata[-1]["error_code"], "stream_aborted")
        self.assertEqual(self.metadata[-1]["upstream_status"], 200)
        self.assertEqual(len(self.upstream.calls), 1, "A partial response must not trigger an automatic retry")

    def test_cooldown_parser_uses_largest_valid_server_delay(self):
        body = json.dumps({"error": {"details": [{"retryDelay": "72.5s"}], "message": "Cooldown wait 47s."}}).encode()
        self.assertEqual(proxy.cooldown_seconds({"Retry-After": "61"}, body), 72.5)
        self.assertEqual(proxy.cooldown_seconds({}, b'{"managerCooldown": 43}'), 43)
        self.assertEqual(proxy.cooldown_seconds({}, b'{"error":{"details":[{"retryDelay":{"seconds":"86","nanos":500000000}}]}}'), 86.5)
        self.assertEqual(proxy.cooldown_seconds({}, b"No cooldown provided"), 0)
        self.assertEqual(proxy.cooldown_seconds({}, b'{"retryDelay":"-5s"}'), 0)
        self.assertEqual(proxy.cooldown_seconds({"Retry-After": formatdate(1065, usegmt=True)}, b"", now_wall=1000), 65)

    def test_invalid_cooldown_cannot_poison_the_gate_or_overflow_sleep(self):
        for value in ("NaN", "inf", "1e308", "-20", "9" * 100):
            with self.subTest(value=value):
                self.assertEqual(proxy.cooldown_seconds({"Retry-After": value}, b""), 0)
                self.assertEqual(proxy.cooldown_seconds({}, json.dumps({"retryDelay": value}).encode()), 0)
        self.assertEqual(proxy.cooldown_seconds({}, b'{"retryDelay":{"seconds":1e308}}'), 0)
        self.assertEqual(proxy.cooldown_seconds({}, json.dumps({"retryDelay": "9" * 100 + "s"}).encode()), 0)
        for value in (float("inf"), float("nan"), 1e308, -5, "invalid"):
            gate = proxy.PacingGate(interval=0, clock=self.clock.monotonic, sleep=self.clock.sleep)
            gate.begin()
            gate.finish(value)
            self.assertFalse(gate.lock.locked())
            self.assertEqual(gate.next_allowed, self.clock.monotonic())
        gate = proxy.PacingGate(interval=0, clock=self.clock.monotonic, sleep=self.clock.sleep)
        gate.begin()
        gate.finish(7201)
        self.assertEqual(gate.begin(), 7201)
        gate.finish()
        self.assertEqual(self.clock.sleeps, [3600, 3600, 1])

    def test_invalid_socket_timeouts_are_rejected_at_configuration(self):
        for value in (0, -1, float("nan"), float("inf"), 1e308):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    proxy.ProxyConfig(FIXTURE_KEY, upstream_timeout=value)
                with self.assertRaises(ValueError):
                    proxy.ProxyConfig(FIXTURE_KEY, request_timeout=value)


if __name__ == "__main__":
    unittest.main()
