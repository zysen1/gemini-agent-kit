#!/usr/bin/env python3
"""Small local GUI and authenticated API facade. Python 3.11+, standard library.

Antigravity Tools is an independently installed, separately licensed component.
This service never reads account tokens from other apps. Explicit token imports
go directly to the selected local Manager; they are not logged or kept here.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paced_proxy import PacingGate, ClientDisconnected, cooldown_seconds

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
MAX_BODY = 8 * 1024 * 1024
MAX_ADMIN = 2 * 1024 * 1024


class AppError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


def private_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise AppError("UNSAFE_PATH", "私有配置不能使用符号链接。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def local_origin(url):
    try:
        u = urllib.parse.urlsplit(url)
        if (u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port
                or u.username or u.password or u.path not in ("", "/")
                or u.query or u.fragment):
            raise ValueError()
        return "http://127.0.0.1:" + str(u.port)
    except ValueError:
        raise AppError("LOCAL_ONLY", "此面板只连接明确的本机 127.0.0.1 服务。") from None


def connection(origin, timeout=15):
    u = urllib.parse.urlsplit(local_origin(origin))
    return http.client.HTTPConnection("127.0.0.1", u.port, timeout=timeout)


def account_view(a):
    email = str(a.get("email", ""))
    label = (email[:1] + "***@" + email.split("@", 1)[1]) if "@" in email else "已导入账号"
    q = a.get("quota") or {}
    return {"id": a.get("id"), "label": label,
            "tier": q.get("subscription_tier") or "待刷新",
            "disabled": bool(a.get("disabled") or a.get("proxy_disabled")),
            "models": [{"id": m.get("name"), "remaining_percent": m.get("percentage")}
                       for m in q.get("models", []) if isinstance(m, dict)
                       and str(m.get("name", "")).startswith("gemini-")]}


def safe_model_header(value):
    return value if isinstance(value, str) and re.fullmatch(r"gemini-[A-Za-z0-9_.-]{1,150}", value) else None


class KitCore:
    def __init__(self, data_dir, port=8787, manager_config=None, inference_origin=None, interval=120):
        from manager_runtime import ManagerRuntime
        self.data_dir = Path(data_dir).expanduser().absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.data_dir.is_symlink():
            raise AppError("UNSAFE_PATH", "数据目录不能使用符号链接。")
        if os.name != "nt":
            os.chmod(self.data_dir, 0o700)
        self.state_path = self.data_dir / "state.json"
        if self.state_path.is_symlink():
            raise AppError("UNSAFE_PATH", "配置文件不能使用符号链接。")
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {
            "schema": 1, "gateway_key": secrets.token_urlsafe(36), "selected_model": None,
            "last_check": None, "last_codex_receipt": None}
        if not isinstance(self.state.get("gateway_key"), str) or len(self.state["gateway_key"]) < 24:
            raise AppError("BAD_STATE", "私有配置损坏，请先保留备份再检查。")
        self.port = port
        self.origin = "http://127.0.0.1:" + str(port)
        self.manager_config = Path(manager_config).expanduser().absolute() if manager_config else None
        self.inference_override = local_origin(inference_origin) if inference_origin else None
        if self.manager_config:
            self.state["manager_config"] = str(self.manager_config)
            self.state["inference_origin"] = self.inference_override
        elif self.state.get("manager_config"):
            self.manager_config = Path(self.state["manager_config"])
            self.inference_override = self.state.get("inference_origin")
        self.runtime = ManagerRuntime(self.data_dir / "runtime", port=8879)
        self.gate = PacingGate(interval=interval)
        self.mutation_lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.save()

    def save(self):
        with self.save_lock:
            private_write(self.state_path, self.state)

    def upstream(self):
        if self.manager_config:
            if self.manager_config.is_symlink() or self.manager_config.stat().st_size > MAX_ADMIN:
                raise AppError("UNSAFE_CONFIG", "指定的 Manager 配置不适合读取。")
            cfg = json.loads(self.manager_config.read_text(encoding="utf-8"))
            p = cfg["proxy"]
            origin = local_origin("http://127.0.0.1:" + str(int(p["port"])))
            key = p["api_key"]
            admin = p.get("admin_password") or key
        else:
            origin = local_origin(self.runtime.describe()["base_url"])
            key, admin = self.runtime.gateway_key, self.runtime.admin_key
        if origin == self.origin or self.inference_override == self.origin:
            raise AppError("SELF_ROUTE", "上游地址不能指向面板自身。")
        if not key or not admin:
            raise AppError("NOT_READY", "请先准备并启动服务。", 503)
        return origin, key, admin

    def request(self, method, path, payload=None, *, admin=True, timeout=20):
        origin, key, admin_key = self.upstream()
        conn = connection(origin, timeout)
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            conn.request(method, path, body=body, headers={"Authorization": "Bearer " + (admin_key if admin else key),
                         "Content-Type": "application/json"})
            res = conn.getresponse()
            raw = res.read(MAX_ADMIN + 1)
            if len(raw) > MAX_ADMIN:
                raise AppError("RESPONSE_SIZE", "服务返回内容过大。", 502)
            if not 200 <= res.status < 300:
                raise AppError("UPSTREAM_" + str(res.status), "服务返回 HTTP " + str(res.status) + "，请检查登录或额度。", 502)
            return json.loads(raw) if raw else {}
        except (OSError, http.client.HTTPException):
            raise AppError("NOT_RUNNING", "服务未启动或暂时没有响应。", 503) from None
        finally:
            conn.close()

    def models(self):
        d = self.request("GET", "/v1/models", admin=False)
        return [{"id": m["id"]} for m in d.get("data", []) if isinstance(m, dict)
                and isinstance(m.get("id"), str) and re.fullmatch(r"gemini-[A-Za-z0-9_.-]{1,150}", m["id"])]

    def exact_model(self, model):
        if model not in [m["id"] for m in self.models()]:
            raise AppError("MODEL_NOT_FOUND", "请选择目录里实际存在的完整 Gemini 型号。")
        return model

    def snapshot(self):
        runtime = {"status": "stopped", "managed": not bool(self.manager_config), "version": None}
        accounts, models, notice = [], [], None
        try:
            runtime.update({"base_url": self.upstream()[0]})
            version = self.request("GET", "/api/health")
            status = self.request("GET", "/api/proxy/status")
            # An empty Manager runs its management API while inference is
            # paused. Keep account import available in that first-run state.
            runtime.update(status="running", proxy_running=bool(status.get("running")), version=version.get("version"))
            d = self.request("GET", "/api/accounts")
            accounts = [account_view(a) for a in (d.get("accounts", []) if isinstance(d, dict) else d)]
            if status.get("running"):
                models = self.models()
            elif accounts:
                notice = "账号已导入，请点击“准备并启动”启用模型服务。"
        except (AppError, OSError, ValueError, KeyError) as e:
            if not self.manager_config:
                runtime.update(self.runtime.describe())
            notice = str(e) if isinstance(e, AppError) else "请先准备服务。"
        configured = False
        try:
            import tomllib
            cfg = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
            provider = cfg.get("model_providers", {}).get("gemini_agent_kit", {})
            configured = provider.get("base_url") == self.origin + "/v1"
        except (OSError, ValueError, TypeError):
            pass
        return {"runtime": runtime, "accounts": accounts, "models": models,
                "selected_model": self.state.get("selected_model"),
                "api": {"base_url": self.origin + "/v1", "key_present": True},
                "codex": {"configured": configured, "desktop_verified": False},
                "last_check": self.state.get("last_check"), "notice": notice}

    def catalog(self):
        models = self.models()
        return {"models": [{"slug": m["id"], "display_name": m["id"],
                  "base_instructions": "Complete the user's task using the available tools. Read provided files before editing. Be accurate and report limits.",
                  "supported_reasoning_levels": [], "shell_type": "shell_command", "visibility": "list",
                  "supported_in_api": True, "priority": i, "support_verbosity": False,
                  "truncation_policy": {"mode": "tokens", "limit": 10000},
                  "experimental_supported_tools": []} for i, m in enumerate(models)]}

    def record_call(self, model, status, elapsed, headers):
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model,
                  "http_status": status, "elapsed_seconds": round(elapsed, 3),
                  "mapped_model": safe_model_header(next((v for k, v in headers.items() if k.lower() == "x-mapped-model"), None)),
                  "upstream_model": safe_model_header(next((v for k, v in headers.items() if k.lower() == "x-upstream-model"), None)),
                  "automatic_retries": 0}
        # Metadata only: never request bodies, account identifiers, auth or output text.
        p = self.data_dir / "calls.jsonl"
        if p.is_symlink():
            return
        fd = os.open(p, os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def check(self, model):
        self.exact_model(model)
        nonce = "kit-" + secrets.token_hex(6)
        origin, key, _ = self.upstream()
        origin = self.inference_override or origin
        self.gate.begin()
        started, conn, cooldown, attempted = time.monotonic(), None, 0, False
        try:
            conn = connection(origin, 240)
            payload = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly this text and nothing else: " + nonce}],
                       "max_tokens": 512, "stream": False}
            attempted = True
            conn.request("POST", "/v1/chat/completions", json.dumps(payload).encode(),
                         {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            res = conn.getresponse()
            raw = res.read(MAX_ADMIN + 1)
            headers = dict(res.getheaders())
            cooldown = cooldown_seconds(headers, raw) if res.status in (429, 503) else 0
            passed, text, finish_reason = False, "", None
            if res.status == 200 and len(raw) <= MAX_ADMIN:
                try:
                    choice = json.loads(raw)["choices"][0]
                    text = choice["message"]["content"]
                    finish_reason = choice.get("finish_reason")
                    passed = isinstance(text, str) and text.strip() == nonce
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
            result = {"passed": passed, "model": model, "http_status": res.status,
                      "summary": "收到符合要求的真实回复；桌面文件任务仍需单独验证。" if passed else
                                 "本次回复未通过内容检查，请检查来源额度或稍后重试。",
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "output_characters": len(text) if isinstance(text, str) else 0,
                      "finish_reason": finish_reason if finish_reason in ("stop", "length", "tool_calls", "content_filter") else None}
            self.record_call(model, res.status, time.monotonic()-started, headers)
            self.state["last_check"] = result
            self.save()
            return result
        except (OSError, http.client.HTTPException):
            self.state["last_check"] = {"passed": False, "model": model, "http_status": None,
                                        "summary": "本次连接没有完成，请检查服务后重试。"}
            self.save()
            raise AppError("CHECK_CONNECTION", "本次连接没有完成，请检查服务后重试。", 502) from None
        finally:
            if conn:
                conn.close()
            self.gate.finish(cooldown) if attempted else self.gate.abandon()

    def mutate(self, path, data):
        if not self.mutation_lock.acquire(blocking=False):
            raise AppError("BUSY", "上一步正在处理，请稍等。", 409)
        try:
            if path == "/api/runtime/setup":
                return {"runtime": self.snapshot()["runtime"]} if self.manager_config else self.runtime.setup()
            if path == "/api/runtime/start":
                if self.manager_config:
                    return self.request("POST", "/api/proxy/start", {})
                result = self.runtime.start()
                accounts = self.request("GET", "/api/accounts")
                if accounts.get("accounts"):
                    self.request("POST", "/api/proxy/start", {})
                return result
            if path == "/api/runtime/stop":
                if self.manager_config:
                    raise AppError("EXTERNAL_SERVICE", "这是复用的现有服务，请在原应用中停止，避免影响其他任务。")
                return self.runtime.stop()
            if path == "/api/accounts":
                token = data.get("refresh_token", "")
                if not isinstance(token, str) or not re.fullmatch(r"1//[A-Za-z0-9_./+-]{20,4096}", token.strip()):
                    raise AppError("TOKEN_FORMAT", "请只输入你已授权账号的完整 Refresh Token（1// 开头）。")
                self.request("POST", "/api/accounts", {"refreshToken": token.strip()}, timeout=90)
                try:
                    self.request("POST", "/api/proxy/start", {})
                except AppError:
                    return {"message": "账号已导入。请点击准备并启动，以启用模型服务。"}
                return {"message": "账号已导入。"}
            if path in ("/api/accounts/refresh", "/api/accounts/remove"):
                ident = data.get("id", "")
                if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                    raise AppError("BAD_ACCOUNT", "账号标识无效。")
                self.request("GET" if path.endswith("refresh") else "DELETE", "/api/accounts/" + ident + ("/quota" if path.endswith("refresh") else ""), timeout=90)
                return {"message": "已更新。"}
            if path == "/api/models/select":
                self.state["selected_model"] = self.exact_model(data.get("model"))
                self.state["last_check"] = None
                self.save()
                return {"model": self.state["selected_model"]}
            if path == "/api/check":
                return self.check(data.get("model") or self.state.get("selected_model"))
            if path.startswith("/api/codex/"):
                import codex_desktop
                cfg = Path.home() / ".codex" / "config.toml"
                if path.endswith("restore"):
                    receipt = self.state.get("last_codex_receipt")
                    if not receipt:
                        raise AppError("NO_BACKUP", "没有本面板写入的配置可恢复。")
                    result = codex_desktop.restore(cfg, receipt)
                    self.state["last_codex_receipt"] = None
                    self.save()
                    return result
                model = self.exact_model(data.get("model") or self.state.get("selected_model"))
                args = dict(config_path=cfg, state_path=self.state_path,
                            base_url=self.origin + "/v1", model=model, activate=False)
                if path.endswith("preview"):
                    return {"preview": codex_desktop.preview(**args)}
                if path.endswith("apply"):
                    before_hash = data.get("expected_before_sha256")
                    if ("expected_before_sha256" not in data
                            or before_hash is not None and (not isinstance(before_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", before_hash))):
                        raise AppError("PREVIEW_REQUIRED", "请先准备配置预览，再确认接入。", 409)
                    result = codex_desktop.apply(**args, expected_before_sha256=before_hash)
                    if result.get("receipt_path"):
                        self.state["last_codex_receipt"] = result["receipt_path"]
                    self.save()
                    return {"receipt": result, "message": "配置已添加。请重启 Codex，选择 Gemini 并新建任务。"}
            raise AppError("NOT_FOUND", "没有这个操作。", 404)
        except Exception as e:
            # These two modules expose deliberately credential-free errors.
            from manager_runtime import RuntimeErrorDetail
            from codex_desktop import ConfigError
            if isinstance(e, RuntimeErrorDetail):
                raise AppError("RUNTIME", str(e), 503) from None
            if isinstance(e, ConfigError):
                raise AppError("CONFIG", "配置未写入：" + str(e), 409) from None
            raise
        finally:
            self.mutation_lock.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # No HTTP query strings, credentials or import bodies in logs.

    @property
    def core(self):
        return self.server.core

    def json_response(self, status, body, cookie=False):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", "kit_session=" + self.server.session + "; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

    def session_ok(self):
        cookies = dict(x.strip().split("=", 1) for x in self.headers.get("Cookie", "").split(";") if "=" in x)
        return hmac.compare_digest(cookies.get("kit_session", ""), self.server.session)

    def gateway_ok(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.core.state["gateway_key"])

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise AppError("BAD_LENGTH", "不支持这种请求编码。", 411)
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY:
            raise AppError("BAD_LENGTH", "请求内容大小不符合要求。", 413)
        if self.headers.get_content_type() != "application/json":
            raise AppError("CONTENT_TYPE", "请求必须使用 JSON。", 415)
        raw = self.rfile.read(size)
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError()
            return d
        except ValueError:
            raise AppError("BAD_JSON", "请求格式不正确。") from None

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        try:
            if not self.valid_host():
                raise AppError("BAD_HOST", "请使用本机启动地址。", 403)
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            if path.startswith("/v1/") or path == "/codex/models":
                if not self.gateway_ok():
                    raise AppError("UNAUTHORIZED", "需要网关密钥。", 401)
                if post and path in ("/v1/responses", "/v1/chat/completions") and not u.query:
                    return self.forward(path, self.body())
                if not post and path == "/v1/models":
                    return self.json_response(200, {"object": "list", "data": self.core.models()})
                if not post and path == "/codex/models":
                    return self.json_response(200, self.core.catalog())
                raise AppError("NOT_FOUND", "不支持这个接口。", 404)
            if u.query:
                raise AppError("BAD_QUERY", "管理接口不接收查询凭据。")
            if path.startswith("/api/"):
                if not self.session_ok():
                    raise AppError("SESSION", "请刷新本机面板后重试。", 401)
                if post:
                    if (self.headers.get("Origin") != self.core.origin
                            or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf)):
                        raise AppError("CSRF", "请求来源不匹配，请刷新面板。", 403)
                    result = self.core.mutate(path, self.body())
                    return self.json_response(200, {"ok": True, **(result if isinstance(result, dict) else {"result": result})})
                if path == "/api/csrf":
                    return self.json_response(200, {"token": self.server.csrf})
                if path == "/api/state":
                    return self.json_response(200, self.core.snapshot())
                raise AppError("NOT_FOUND", "没有这个接口。", 404)
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css")}
            if post or path not in files:
                raise AppError("NOT_FOUND", "没有这个页面。", 404)
            name, mime = files[path]
            raw = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            if path == "/":
                self.send_header("Set-Cookie", "kit_session=" + self.server.session + "; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ClientDisconnected):
            self.close_connection = True
        except AppError as e:
            self.close_connection = True
            self.json_response(e.status, {"ok": False, "error": {"code": e.code, "message": str(e)}})
        except Exception:
            self.close_connection = True
            self.json_response(500, {"ok": False, "error": {"code": "LOCAL_ERROR", "message": "本机操作未完成，请检查运行组件与配置；没有记录你的凭据。"}})

    def disconnected(self):
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def forward(self, path, payload):
        model = self.core.exact_model(payload.get("model"))
        origin, key, _ = self.core.upstream()
        origin = self.core.inference_override or origin
        conn, attempted, sent, cooldown = None, False, False, 0
        self.core.gate.begin(cancelled=self.disconnected)
        started = time.monotonic()
        try:
            conn = connection(origin, 300)
            attempted = True
            conn.request("POST", path, json.dumps(payload).encode(),
                         {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            res = conn.getresponse()
            headers = dict(res.getheaders())
            if not 200 <= res.status < 300:
                # Never pass upstream diagnostic bodies to downstream clients.
                # They can contain account identifiers or upstream credentials.
                raw = res.read(MAX_ADMIN + 1)
                cooldown = cooldown_seconds(headers, raw) if res.status in (429, 503) else 0
                self.send_response(res.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                if cooldown:
                    self.send_header("Retry-After", str(math.ceil(cooldown)))
                self.end_headers()
                self.close_connection, sent = True, True
                self.wfile.write(json.dumps({"error": {"message": "Upstream request did not complete (HTTP " + str(res.status) + "). Check account access or quota.", "type": "upstream_error"}}).encode())
                self.core.record_call(model, res.status, time.monotonic()-started, headers)
                return
            self.send_response(res.status)
            for name, value in res.getheaders():
                if (name.lower() == "content-type"
                        or name.lower() in ("x-mapped-model", "x-upstream-model") and safe_model_header(value)):
                    self.send_header(name, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection, sent = True, True
            prefix = b""
            while True:
                chunk = res.read1(65536)
                if not chunk:
                    break
                if len(prefix) < 8192:
                    prefix += chunk[:8192-len(prefix)]
                self.wfile.write(chunk)
                self.wfile.flush()
            cooldown = cooldown_seconds(headers, prefix) if res.status in (429, 503) else 0
            self.core.record_call(model, res.status, time.monotonic()-started, headers)
        except (OSError, http.client.HTTPException):
            if not sent:
                self.json_response(502, {"error": {"message": "模型来源暂时没有响应。", "type": "upstream_error"}})
        finally:
            if conn:
                conn.close()
            self.core.gate.finish(cooldown) if attempted else self.core.gate.abandon()


def make_server(core, port=8787):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.core, server.csrf, server.session = core, secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    core.port = server.server_port
    core.origin = "http://127.0.0.1:" + str(core.port)
    return server


def default_data_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Gemini Agent Kit"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Gemini Agent Kit"
    return Path.home() / ".local/share/gemini-agent-kit"


def main():
    parser = argparse.ArgumentParser(description="Gemini Agent Kit 本机面板（Python 3.11+）")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--connect-manager-config", type=Path, help="明确复用已授权的本机 Manager 配置；只读取网关字段")
    parser.add_argument("--inference-origin", help="可选：已存在的本机串行代理，例如 http://127.0.0.1:8046")
    parser.add_argument("--open", action="store_true", help="启动后用默认浏览器打开面板")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("图形面板需要 Python 3.11 或更新版本。")
    core = KitCore(args.data_dir, args.port, args.connect_manager_config, args.inference_origin)
    server = make_server(core, args.port)
    print("Gemini Agent Kit 已启动：" + core.origin, flush=True)
    print("只在本机开放；Ctrl+C 关闭面板。没有自动发起模型请求。", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(core.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if not core.manager_config:
            core.runtime.stop()


if __name__ == "__main__":
    main()
