#!/usr/bin/env python3
"""Loopback-only pacing proxy. No retries, model changes, or account changes."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
import email.utils
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import threading
import time
from typing import Callable
import urllib.parse

KEY_ENV = "GEMINI_AGENT_GATEWAY_KEY"
LOOPBACK = "127.0.0.1"
UPSTREAM_PORT = 8045
LISTEN_PORT = 8046
MAX_REQUEST = 32 * 1024 * 1024
MAX_ERROR_PREFIX = 64 * 1024
MAX_COOLDOWN_TIMESTAMP = 253402300799  # Last full second of datetime year 9999.
POST_ROUTES = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}
GET_ROUTES = {"/v1/models", "/v1/version"}
HEALTH_ROUTES = {"/healthz", "/readyz"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def allowed_route(method, target):
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    allowed = GET_ROUTES | HEALTH_ROUTES if method == "GET" else POST_ROUTES
    if parsed.path not in allowed:
        return None
    if parsed.query:
        if method != "GET" or parsed.path != "/v1/models":
            return None
        try:
            params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return None
        if len(params) != 1 or params[0][0] != "client_version" or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", params[0][1]):
            return None
    return parsed.path


def duration_seconds(value):
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, dict):
        try:
            result = float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9
        except (TypeError, ValueError, OverflowError):
            return 0.0
    else:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:s|seconds?|secs?)?\s*", str(value), re.I)
        if not match:
            return 0.0
        result = float(match.group(1))
    return result if math.isfinite(result) and result >= 0 else 0.0


def cooldown_seconds(headers, body: bytes, now_wall=None):
    """Parse delays only; callers apply this to HTTP 429/503, never to success."""
    now_wall = time.time() if now_wall is None else now_wall
    items = headers.items() if hasattr(headers, "items") else headers
    values = {str(key).lower(): str(value) for key, value in items}
    candidates = [0.0]
    retry_after = values.get("retry-after", "")
    numeric = duration_seconds(retry_after)
    if numeric:
        candidates.append(numeric)
    elif retry_after:
        try:
            deadline = email.utils.parsedate_to_datetime(retry_after)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=dt.timezone.utc)
            stamp = deadline.timestamp()
            candidates.append(max(0.0, stamp - now_wall))
        except (ValueError, TypeError, OverflowError):
            pass
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None

    def walk(item, depth=0):
        if depth > 20:
            return
        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).lower().replace("_", "") in {"retrydelay", "managercooldown", "cooldown", "cooldownseconds"}:
                    candidates.append(duration_seconds(value))
                walk(value, depth + 1)
        elif isinstance(item, list):
            for value in item:
                walk(value, depth + 1)

    walk(data)
    for match in re.finditer(r"(?:cooldown(?:\s+wait)?|retry\s+(?:after|in))\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec(?:ond)?s?\b)", text, re.I):
        candidates.append(duration_seconds(match.group(1)))
    # Reject malformed/unrepresentable deadlines rather than poisoning the
    # shared queue with infinity or an overflowing sleep. Real deadlines within
    # the timestamp range are preserved; do not shorten a legitimate cooldown.
    return max((value for value in candidates if math.isfinite(value)
                and 0 <= value < MAX_COOLDOWN_TIMESTAMP - now_wall), default=0.0)


class ClientDisconnected(Exception):
    """The waiting client closed its socket before an upstream attempt."""


class PacingGate:
    """The lock covers queue wait, the upstream request, and its whole stream."""
    def __init__(self, interval=35.0, clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(interval) or not 0 <= interval < MAX_COOLDOWN_TIMESTAMP - time.time():
            raise ValueError("Invalid pacing interval")
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def begin(self, cancelled=None):
        # Poll both lock contention and cooldown. A cancelled queued request must
        # not wait minutes and then become a new paid upstream request.
        if cancelled is None:
            self.lock.acquire()
        else:
            while True:
                if cancelled():
                    raise ClientDisconnected()
                if self.lock.acquire(timeout=0.25):
                    break
        waited = 0.0
        try:
            while True:
                if cancelled is not None and cancelled():
                    raise ClientDisconnected()
                delay = self.next_allowed - self.clock()
                if delay <= 0:
                    break
                if cancelled is not None:
                    delay = min(delay, 0.25)
                else:
                    # Long valid deadlines must not overflow OS sleep limits.
                    delay = min(delay, 3600.0)
                self.sleep(delay)
                waited += delay
            return waited
        except BaseException:
            self.lock.release()
            raise

    def finish(self, cooldown=0.0):
        try:
            try:
                cooldown = float(cooldown)
                if not math.isfinite(cooldown) or not 0 <= cooldown < MAX_COOLDOWN_TIMESTAMP - time.time():
                    cooldown = 0.0
            except (TypeError, ValueError, OverflowError):
                cooldown = 0.0
            self.next_allowed = self.clock() + max(self.interval, cooldown)
        finally:
            self.lock.release()

    def abandon(self):
        """Release an acquired slot without changing an existing cooldown."""
        self.lock.release()


@dataclass
class ProxyConfig:
    key: str = field(repr=False)
    upstream_port: int = UPSTREAM_PORT
    interval: float = 35.0
    metadata_log: Callable | None = None
    gate: PacingGate | None = None
    upstream_timeout: float = 600.0
    request_timeout: float = 30.0

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key or not self.key.isascii() or any(char in self.key for char in "\r\n"):
            raise ValueError("A nonempty gateway key without line breaks is required")
        if not 1 <= self.upstream_port <= 65535:
            raise ValueError("Invalid loopback upstream port")
        if not math.isfinite(self.upstream_timeout) or not 0 < self.upstream_timeout <= threading.TIMEOUT_MAX:
            raise ValueError("Invalid upstream timeout")
        if not math.isfinite(self.request_timeout) or not 0 < self.request_timeout <= 600:
            raise ValueError("Invalid client request timeout")
        if self.gate is None:
            self.gate = PacingGate(self.interval)


def filtered_headers(pairs):
    pairs = list(pairs)
    blocked = set(HOP_HEADERS) | {"content-length", "host", "cookie", "cookie2"}
    for key, value in pairs:
        if key.lower() == "connection":
            blocked.update(item.strip().lower() for item in value.split(","))
    return [(key, value) for key, value in pairs if key.lower() not in blocked]


def safe_response_headers(pairs):
    """Only protocol content metadata and bounded Gemini routing labels.

    Do not forward cookies, auth challenges, debug headers or arbitrary upstream
    fields, including on successful responses. Model labels are routing claims,
    not verification of the underlying model. Error responses use no such headers.
    """
    media_types = {"application/json", "application/problem+json", "application/x-ndjson",
                   "application/ndjson", "application/json-seq", "application/octet-stream",
                   "text/event-stream", "text/plain"}
    model_headers = {"x-model-id", "x-actual-model", "x-mapped-model", "x-upstream-model"}
    result, seen = [], set()
    for key, value in filtered_headers(pairs):
        name = key.lower()
        if name in seen:
            continue
        if name == "content-type":
            # Keep only the MIME type and a recognized charset, not custom
            # parameters which can themselves contain diagnostic information.
            media, _, parameters = value.partition(";")
            media = media.strip().lower()
            if media not in media_types:
                continue
            charset = re.search(r'(?:^|;)\s*charset\s*=\s*"?(utf-8|utf8|us-ascii)"?\s*(?:;|$)', parameters, re.I)
            value = media + ("; charset=" + charset.group(1).lower() if charset else "")
        elif name == "content-encoding":
            encodings = [item.strip().lower() for item in value.split(",")]
            if not encodings or any(item not in {"identity", "gzip", "deflate", "br", "zstd"} for item in encodings):
                continue
            value = ", ".join(encodings)
        elif name in model_headers:
            if not re.fullmatch(r"(?:models/)?gemini-[A-Za-z0-9_.-]{1,150}", value):
                continue
        else:
            continue
        seen.add(name)
        result.append((key, value))
    return result


class MetadataLog:
    """Only fixed metadata fields are emitted, never headers, bodies, or tokens."""
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(path, flags, 0o600)
        self.lock = threading.Lock()

    def __call__(self, metadata):
        raw = (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.lock:
            os.write(self.fd, raw)

    def close(self):
        os.close(self.fd)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GeminiPacedProxy/0.1"

    def setup(self):
        super().setup()
        # Bound partial request headers/bodies and slow downstream writes;
        # this does not expire queue waits or change the upstream timeout.
        self.connection.settimeout(self.server.proxy_config.request_timeout)

    def log_message(self, *_):
        pass

    @property
    def config(self):
        return self.server.proxy_config

    def send_json(self, status, code, *, retry_after=0.0, **fields):
        data = json.dumps({"code": code, **fields}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if retry_after:
            self.send_header("Retry-After", str(math.ceil(retry_after)))
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def client_disconnected(self):
        """Best-effort EOF/RST detection; a TCP half-close counts as cancel."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except (OSError, ValueError):
            return True

    def dispatch(self, method):
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            self.send_json(403, "loopback_only")
            return
        auth = self.headers.get_all("Authorization", [])
        expected = ("Bearer " + self.config.key).encode()
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected):
            self.send_json(401, "unauthorized")
            return
        self.route_path = allowed_route(method, self.path)
        if self.route_path is None:
            self.send_json(404, "route_not_allowed")
            return
        if method == "GET" and self.path in HEALTH_ROUTES:
            self.send_json(200, "ready", loopback_only=True, interval_seconds=self.config.interval,
                           upstream_checked=False, automatic_retries=0)
            return
        body = None
        if method == "POST":
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                self.send_json(411, "valid_content_length_required")
                return
            length = int(lengths[0])
            if length > MAX_REQUEST:
                self.send_json(413, "request_too_large")
                return
            try:
                body = self.rfile.read(length)
            except (socket.timeout, TimeoutError):
                self.send_json(408, "request_timeout")
                return
            except OSError:
                self.close_connection = True
                return
            if len(body) != length:
                self.send_json(400, "incomplete_request")
                return
        self.forward(method, body)

    def forward(self, method, body):
        started = time.monotonic()
        gate_held = False
        pacing_wait = 0.0
        queue_wait = 0.0
        cooldown = 0.0
        upstream_status = None
        status = None
        response_started = False
        response_bytes = 0
        error = None
        connection = None
        response = None
        upstream_attempted = False
        try:
            if method == "POST":
                pacing_wait = self.config.gate.begin(cancelled=self.client_disconnected)
                gate_held = True
                queue_wait = time.monotonic() - started
            connection = http.client.HTTPConnection(LOOPBACK, self.config.upstream_port, timeout=self.config.upstream_timeout)
            headers = dict(filtered_headers(self.headers.items()))
            # Use only the validated key. Never inherit other proxy credentials.
            headers.pop("Authorization", None)
            for key in list(headers):
                if key.lower() == "authorization":
                    headers.pop(key)
            headers["Authorization"] = "Bearer " + self.config.key
            headers["Connection"] = "close"
            if method == "POST" and self.client_disconnected():
                raise ClientDisconnected()
            upstream_attempted = True
            connection.request(method, self.path, body=body, headers=headers)
            response = connection.getresponse()
            upstream_status = response.status
            if 300 <= response.status < 400:
                status = 502
                error = "upstream_redirect_blocked"
                self.send_json(status, error)
                return
            status = response.status
            if not 200 <= status < 300:
                # Error diagnostics can contain refresh tokens, account email,
                # cookies and debug headers. Inspect a bounded prefix only to
                # extract an explicit delay; none of it goes to the client/log.
                error = "upstream_error"
                if status in (429, 503):
                    response_headers = response.getheaders()
                    cooldown = cooldown_seconds(response_headers, b"")
                    prefix = bytearray()
                    try:
                        while len(prefix) < MAX_ERROR_PREFIX:
                            chunk = response.read1(MAX_ERROR_PREFIX - len(prefix))
                            if not chunk:
                                break
                            prefix.extend(chunk)
                    except (OSError, http.client.HTTPException):
                        # Preserve the known HTTP failure and any header delay
                        # even when its diagnostic body is partial/unavailable.
                        pass
                    cooldown = max(cooldown, cooldown_seconds(response_headers, bytes(prefix)))
                self.send_json(status, "upstream_error", retry_after=cooldown,
                               message="Upstream request did not complete. Check account access or quota.")
                response_started = True
                return
            self.send_response_only(status)
            for key, value in safe_response_headers(response.getheaders()):
                self.send_header(key, value)
            no_body = status in (204, 304)
            self.send_header("Content-Length" if no_body else "Transfer-Encoding", "0" if no_body else "chunked")
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            response_started = True
            if not no_body:
                while True:
                    # read1 returns available bytes; read(n) can hide SSE chunks
                    # until n bytes or EOF, preventing clients from seeing tokens.
                    data = response.read1(64 * 1024)
                    if not data:
                        if response.length not in (None, 0):
                            raise http.client.IncompleteRead(b"")
                        break
                    self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                    response_bytes += len(data)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except ClientDisconnected:
            queue_wait = time.monotonic() - started
            error = "client_disconnected_before_upstream"
        except (socket.timeout, TimeoutError):
            error = "upstream_timeout" if not response_started else "stream_timeout"
            if not response_started:
                status = 504
                self.send_json(status, error)
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            error = "transport_error" if not response_started else "stream_aborted"
            if not response_started:
                status = 502
                self.send_json(status, error)
        finally:
            self.close_connection = True
            if response:
                response.close()
            if connection:
                connection.close()
            if gate_held:
                if upstream_attempted:
                    self.config.gate.finish(cooldown)
                else:
                    self.config.gate.abandon()
            if self.config.metadata_log:
                # Do not add exception messages, request IDs from clients, model
                # headers, credentials, prompts, or response contents here.
                metadata = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "method": method, "route": self.route_path, "status": status, "upstream_status": upstream_status,
                            "queue_wait_seconds": round(queue_wait, 3), "pacing_sleep_seconds": round(pacing_wait, 3),
                            "elapsed_seconds": round(time.monotonic() - started, 3), "response_bytes": response_bytes,
                            "next_minimum_wait_seconds": max(self.config.interval, cooldown) if method == "POST" and upstream_attempted else None,
                            "upstream_attempted": upstream_attempted,
                            "upstream_cooldown_seconds": cooldown, "automatic_retries": 0, "error_code": error}
                try:
                    self.config.metadata_log(metadata)
                except OSError:
                    # A failed metadata disk write must not alter the model reply.
                    pass


def make_server(config: ProxyConfig, port=LISTEN_PORT):
    # Alternate ports exist for local fixture tests only; the CLI fixes both ports.
    server = ThreadingHTTPServer((LOOPBACK, port), ProxyHandler)
    server.daemon_threads = True
    server.proxy_config = config
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=35.0, help="Minimum seconds after each inference request completes (at least 35)")
    parser.add_argument("--upstream-timeout", type=float, default=600.0, help="Upstream socket timeout; includes time waiting for its response")
    parser.add_argument("--metadata-log", default="reports/paced-proxy-metadata.jsonl", help="Append-only metadata JSONL; never includes request/response bodies or auth")
    args = parser.parse_args(argv)
    log = None
    server = None
    try:
        if not math.isfinite(args.interval) or args.interval < 35:
            raise ValueError("CLI pacing interval must be at least 35 seconds")
        config = ProxyConfig(key=os.environ.get(KEY_ENV, ""), interval=args.interval, upstream_timeout=args.upstream_timeout)
        log = MetadataLog(args.metadata_log)
        config.metadata_log = log
        server = make_server(config)
        print(json.dumps({"status": "listening", "listen": "http://127.0.0.1:8046", "upstream": "http://127.0.0.1:8045", "interval_seconds": args.interval, "automatic_retries": 0}), file=__import__("sys").stderr)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        print('{"error":"startup_failed","detail":"Check gateway-key environment, interval, port availability, timeout, and log path."}', file=__import__("sys").stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
