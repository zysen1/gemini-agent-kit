#!/usr/bin/env python3
"""Inspect an existing gateway and connect clients without changing their defaults.

Only the Python standard library is required (Python 3.10+).
No subscription credentials, browser sessions, or provider configuration are read.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
KEY_ENV = "GEMINI_AGENT_GATEWAY_KEY"
DEFAULT_URL = "http://127.0.0.1:3425/v1"
LOCAL_CLIENT_LABEL = "magpie-agent-kit"
MAX_RESPONSE = 4 * 1024 * 1024
MAX_MCP_LINE = 1024 * 1024


class KitError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def redact(value: Any, secret: str = "") -> Any:
    """Redact credentials even if an upstream mistakenly echoes them."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.search(r"^(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)$", str(key), re.I):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item, secret)
        return result
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str):
        if secret and secret != LOCAL_CLIENT_LABEL:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[^\s\"',<>]+", "Bearer [REDACTED]", value)
        return value
    return value


def loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_url(url: str, allow_insecure: bool = False) -> tuple[str, bool]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise KitError("INVALID_URL", "Invalid gateway URL or port.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise KitError("INVALID_URL", "Gateway URL must use http:// or https:// and include a host.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise KitError("INVALID_URL", "Do not put credentials, query parameters, or fragments in the gateway URL.")
    if any(c.isspace() for c in url) or "\\" in url:
        raise KitError("INVALID_URL", "Whitespace and backslashes are not allowed in the gateway URL.")
    if port is not None and not 1 <= port <= 65535:
        raise KitError("INVALID_URL", "Gateway port must be between 1 and 65535.")
    local = loopback_host(parsed.hostname)
    if parsed.scheme == "http" and not local and not allow_insecure:
        raise KitError("INSECURE_GATEWAY", "Remote gateways require HTTPS. Use --allow-insecure-http only for an explicitly trusted network.")
    path = parsed.path.rstrip("/") or "/v1"
    if any(part in (".", "..") for part in urllib.parse.unquote(path).split("/")):
        raise KitError("INVALID_URL", "Gateway path must not contain dot segments.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")), local


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the gateway credential to a redirect target.


class Gateway:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 30,
                 allow_insecure: bool = False, environ: dict | None = None):
        self.base_url, self.local = normalize_url(base_url, allow_insecure)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
            raise KitError("INVALID_TIMEOUT", "Timeout must be greater than zero and at most 600 seconds.")
        self.timeout = timeout
        env = os.environ if environ is None else environ
        self.key = env.get(KEY_ENV, "").strip()
        if not self.key:
            if self.local:
                self.key = LOCAL_CLIENT_LABEL
            else:
                raise KitError("MISSING_GATEWAY_KEY", f"Set {KEY_ENV} in the process environment for a remote gateway.")
        if any(c in self.key for c in "\r\n"):
            raise KitError("INVALID_GATEWAY_KEY", "Gateway key must not contain line breaks.")
        # A local endpoint must not travel through an HTTP proxy from the environment.
        handlers = [NoRedirect()]
        if self.local:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, path: str, payload: dict | None = None, *, origin: bool = False) -> dict:
        if origin:
            u = urllib.parse.urlsplit(self.base_url)
            url = urllib.parse.urlunsplit((u.scheme, u.netloc, path, "", ""))
        else:
            url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "gemini-agent-kit/" + VERSION,
        }, method="GET" if data is None else "POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise KitError("RESPONSE_TOO_LARGE", "Gateway response exceeded 4 MiB.")
        except urllib.error.HTTPError as exc:
            try:
                try:
                    raw = exc.read(4096).decode("utf-8", "replace")
                except (OSError, http.client.HTTPException):
                    raw = "[error response body unavailable]"
            finally:
                exc.close()
            detail = redact(raw, self.key)[:1200]
            raise KitError("HTTP_ERROR", f"Gateway HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise KitError("TIMEOUT", f"Gateway request timed out after {self.timeout:g} seconds.") from None
            raise KitError("NETWORK_ERROR", redact(str(exc), self.key)) from None
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise KitError("INVALID_RESPONSE", "Gateway did not return valid JSON.") from None
        if not isinstance(result, dict):
            raise KitError("INVALID_RESPONSE", "Expected a JSON object from the gateway.")
        if "error" in result:
            raise KitError("UPSTREAM_ERROR", json.dumps(redact(result["error"], self.key), ensure_ascii=False)[:1200])
        return result

    def models(self) -> list[dict]:
        result = self.request("/models")
        data = result.get("data")
        if not isinstance(data, list):
            raise KitError("INVALID_CATALOG", "The /models response must contain a data array.")
        clean = []
        seen = set()
        for model in data:
            if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"] and model["id"] not in seen and not any(ord(c) < 32 or ord(c) == 127 for c in model["id"]):
                seen.add(model["id"])
                clean.append({key: model[key] for key in ("id", "owned_by", "display_name", "context_window", "supported_reasoning_levels") if key in model})
        return clean

    def chat(self, model: str, messages: list, *, max_tokens: int = 256, **extra) -> dict:
        return self.request("/chat/completions", {
            "model": model, "messages": messages, "stream": False,
            "max_tokens": max_tokens, **extra,
        })


def select_model(catalog: list[dict], requested: str | None) -> str:
    ids = [m["id"] for m in catalog]
    if requested:
        if requested not in ids:
            raise KitError("MODEL_NOT_FOUND", "Requested model is absent from the gateway catalog. Run models and copy an exact ID; no fallback was attempted.")
        return requested
    candidates = [m for m in ids if "gemini" in m.lower() and "argon" in m.lower()]
    if len(candidates) == 1:
        return candidates[0]
    raise KitError("MODEL_REQUIRED", "Pass --model with an exact ID from models. Automatic selection requires exactly one Gemini Argon entry; no fallback was attempted.")


def message_of(response: dict) -> dict:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
        raise KitError("INVALID_CHAT_RESPONSE", "Missing choices[0].message in the response.")
    return choices[0]["message"]


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def save_json(path: str, value: dict, secret: str = "") -> str:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise KitError("OUTPUT_EXISTS", "Output already exists. Choose a new filename; existing reports are not overwritten.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(redact(value, secret), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return str(target.resolve())


def check_new_output(path: str) -> None:
    target = Path(path).expanduser()
    # lexists also catches dangling symlinks. Recheck atomically when writing.
    if os.path.lexists(target):
        raise KitError("OUTPUT_EXISTS", "Output already exists. Choose a new filename; existing reports are not overwritten.")
    if target.parent.exists() and not target.parent.is_dir():
        raise KitError("INVALID_OUTPUT", "The output parent must be a directory.")


def doctor(gateway: Gateway) -> dict:
    report = {"kit_version": VERSION, "python": platform.python_version(),
              "platform": platform.system(), "architecture": platform.machine(),
              "gateway": gateway.base_url, "loopback": gateway.local,
              "credential_source": KEY_ENV if gateway.key != LOCAL_CLIENT_LABEL else "local Magpie client label",
              "codex_on_path": shutil.which("codex") is not None,
              "inference_requested": False, "gateway_reachable": False,
              "model_count": None, "catalog_has_models": None,
              "ok_means": "The gateway returned a valid model catalog; inference has not been tested."}
    try:
        hello = gateway.request("/api/hello", origin=True)
        report["gateway_reachable"] = True
        report["magpie_hello"] = {"name": hello.get("name"), "version": hello.get("version")}
    except KitError as exc:
        if exc.code in ("HTTP_ERROR", "UPSTREAM_ERROR", "INVALID_RESPONSE", "RESPONSE_TOO_LARGE"):
            report["gateway_reachable"] = True
        report["magpie_hello"] = {"available": False, "code": exc.code,
                                  "note": "Compatible gateways may not implement Magpie's hello endpoint."}
    try:
        report["models"] = gateway.models()
        report["gateway_reachable"] = True
        report["model_count"] = len(report["models"])
        report["catalog_has_models"] = bool(report["models"])
        report["ok"] = True
        if not report["models"]:
            report["warning"] = "Gateway is reachable but its model catalog is empty. Configure an authorized model source in the gateway before plan, verify, or model calls."
    except KitError as exc:
        if exc.code in ("HTTP_ERROR", "UPSTREAM_ERROR", "INVALID_RESPONSE", "INVALID_CATALOG", "RESPONSE_TOO_LARGE"):
            report["gateway_reachable"] = True
        report["ok"] = False
        report["error"] = {"code": exc.code, "message": str(exc)}
    return redact(report, gateway.key)


def make_plan(gateway: Gateway, model: str, client: str, runtime_script: str | None = None) -> dict:
    plan = {"client": client, "model": model, "gateway": gateway.base_url,
            "status": "configuration_plan_only", "credentials_included": False,
            "credential_environment_variable": KEY_ENV,
            "model_source": "exact entry from gateway /models; entitlement is not inferred from its name",
            "verification": ["Run verify against the configured gateway.",
                             "Then test a real task and a changed input inside this client; a gateway test is not client end-to-end validation."]}
    if client == "codex":
        plan["mode"] = "model_provider"
        plan["surface"] = "Codex CLI; desktop model menus are not configured"
        plan["provider"] = {"name": "Gemini Agent Kit gateway", "base_url": gateway.base_url,
                            "env_key": KEY_ENV, "wire_api": "responses"}
        plan["next_action"] = "Use launch-codex for one process; this kit does not modify global Codex settings."
    elif client == "workbuddy":
        plan["mode"] = "custom_model"
        plan["ui_path"] = "Settings → Models → Add model → Custom"
        plan["fields"] = {"api_url": gateway.base_url + "/chat/completions", "model_name": model,
                          "api_key_field_instruction": f"Enter the value of {KEY_ENV} privately; local Magpie accepts a client label.",
                          "tool_calling": True}
        plan["next_action"] = "Fill the current app's custom-provider form, test, and save only after a real response. UI versions may differ."
    else:
        plan["mode"] = "external_tool_delegation"
        script = runtime_script or "/path/on/agent-computer/gemini-agent-kit/scripts/agent_kit.py"
        mcp_args = [script, "mcp", "--base-url", gateway.base_url, "--model", model, "--timeout", str(gateway.timeout)]
        if gateway.base_url.startswith("http://") and not gateway.local:
            mcp_args.append("--allow-insecure-http")
        plan["mcp_server"] = {"command": "python3", "args": mcp_args,
                              "required_environment_variable_names": [KEY_ENV] if gateway.key != LOCAL_CLIENT_LABEL else []}
        plan["tool"] = "ask_gemini"
        plan["capability_requirement"] = "Confirm that this agent supports local commands or stdio MCP before applying; a product name alone does not establish support."
        plan["next_action"] = "Copy this repository to the agent's runtime. Configure a private command/MCP connector using its supported UI and secure credential mechanism."
        plan["host_boundary"] = "A cloud computer cannot use your Mac's localhost. Run the gateway in the connector's environment, or supply its authenticated HTTPS endpoint."
        plan["limitation"] = "The original agent still plans and controls permissions. Adding a Gemini tool does not replace its main model."
    return plan


def verify(gateway: Gateway, model: str) -> dict:
    start = time.monotonic()
    report = {"kit_version": VERSION, "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "gateway": gateway.base_url, "requested_model": model,
              "scope": "HTTP requests to the configured gateway; upstream model identity is not independently proven",
              "client_end_to_end_verified": False, "tests": [], "request_count": 0,
              "request_count_basis": "Attempted inference requests, including failed requests."}
    marker = "KIT_TEXT_" + uuid.uuid4().hex[:12]
    try:
        report["request_count"] += 1
        reply = gateway.chat(model, [{"role": "user", "content": "Reply with exactly this string and nothing else: " + marker}], max_tokens=128)
        output = text_of(message_of(reply)).strip()
        report["tests"].append({"name": "short_text", "passed": output == marker,
                                "provider_reported_model": reply.get("model"), "usage": reply.get("usage"),
                                "response_preview": output[:500]})
    except KitError as exc:
        report["tests"].append({"name": "short_text", "passed": False, "error": {"code": exc.code, "message": str(exc)}})
    nonce = uuid.uuid4().hex
    tool = {"type": "function", "function": {"name": "kit_probe", "description": "Return a probe result for the exact given nonce.",
            "parameters": {"type": "object", "properties": {"nonce": {"type": "string"}}, "required": ["nonce"], "additionalProperties": False}}}
    messages = [{"role": "user", "content": f"Call kit_probe with nonce {nonce}. After receiving its result, reply with the proof string exactly."}]
    try:
        report["request_count"] += 1
        reply = gateway.chat(model, messages, tools=[tool], tool_choice={"type": "function", "function": {"name": "kit_probe"}}, max_tokens=256)
        assistant = message_of(reply)
        calls = assistant.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise KitError("TOOL_CALL_MISSING", "Expected exactly one kit_probe tool call.")
        call = calls[0]
        if not isinstance(call, dict):
            raise KitError("TOOL_CALL_INVALID", "Tool call must be a JSON object.")
        fn = call.get("function", {})
        if not isinstance(fn, dict) or fn.get("name") != "kit_probe" or not isinstance(call.get("id"), str) or not call["id"]:
            raise KitError("TOOL_CALL_INVALID", "Wrong function name or missing call ID.")
        try:
            arguments = json.loads(fn.get("arguments", ""))
        except (json.JSONDecodeError, TypeError):
            raise KitError("TOOL_ARGUMENTS_INVALID", "Tool arguments were not valid JSON.") from None
        if arguments != {"nonce": nonce}:
            raise KitError("TOOL_ARGUMENTS_INVALID", "Tool did not pass the exact nonce and expected schema.")
        proof = hashlib.sha256(nonce.encode()).hexdigest()
        # Preserve provider-specific assistant metadata (e.g. Gemini thought signatures).
        messages += [assistant, {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"proof": proof})}]
        report["request_count"] += 1
        final = gateway.chat(model, messages, tools=[tool], tool_choice="none", max_tokens=256)
        answer = text_of(message_of(final)).strip()
        report["tests"].append({"name": "tool_round_trip", "passed": answer == proof,
                                "function_executed": "local deterministic kit_probe; no shell or files",
                                "provider_reported_model": final.get("model"),
                                "tool_call_usage": reply.get("usage"), "final_response_usage": final.get("usage"),
                                "response_preview": answer[:500]})
    except KitError as exc:
        report["tests"].append({"name": "tool_round_trip", "passed": False, "error": {"code": exc.code, "message": str(exc)}})
    report["passed"] = all(test["passed"] for test in report["tests"])
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    report["interpretation"] = "Passing proves this gateway's text and function-message round trip for these probes, not all agents, all tasks, unlimited quota, or model provenance."
    return redact(report, gateway.key)


TOOL_SCHEMA = {"name": "ask_gemini", "description": "Send a bounded text task to the explicitly configured Gemini gateway model.",
               "inputSchema": {"type": "object", "properties": {
                   "prompt": {"type": "string", "minLength": 1, "maxLength": 64000},
                   "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192, "default": 1024}},
                   "required": ["prompt"], "additionalProperties": False}}


def mcp_serve(gateway: Gateway, requested_model: str | None, inp=None, out=None):
    inp = inp or sys.stdin
    out = out or sys.stdout
    initialized = False
    selected = None

    def send(value):
        out.write(json.dumps(redact(value, gateway.key), ensure_ascii=False) + "\n")
        out.flush()

    while True:
        line = inp.readline(MAX_MCP_LINE + 1)
        if not line:
            return
        if len(line) > MAX_MCP_LINE:
            while line and not line.endswith("\n"):
                line = inp.readline(MAX_MCP_LINE + 1)
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Message too large."}})
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON."}})
            continue
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request."}})
            continue
        if "id" not in msg:
            continue
        rid = msg["id"]
        method = msg["method"]
        params = msg.get("params", {})
        try:
            if not isinstance(params, dict):
                raise KitError("INVALID_PARAMS", "Parameters must be an object.")
            if method == "initialize":
                requested = params.get("protocolVersion")
                versions = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                result = {"protocolVersion": requested if isinstance(requested, str) and requested in versions else "2024-11-05",
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "gemini-agent-kit", "version": VERSION}}
                initialized = True
            elif method == "ping":
                result = {}
            elif not initialized:
                raise KitError("NOT_INITIALIZED", "Send initialize first.")
            elif method == "tools/list":
                result = {"tools": [TOOL_SCHEMA]}
            elif method == "tools/call":
                if params.get("name") != "ask_gemini":
                    raise KitError("INVALID_PARAMS", "Unknown tool.")
                args = params.get("arguments", {})
                if not isinstance(args, dict) or set(args) - {"prompt", "max_tokens"}:
                    raise KitError("INVALID_PARAMS", "Unexpected tool arguments.")
                prompt, limit = args.get("prompt"), args.get("max_tokens", 1024)
                if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 64000 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8192:
                    raise KitError("INVALID_PARAMS", "Provide a nonempty prompt (max 64000 characters) and max_tokens from 1 to 8192.")
                try:
                    if selected is None:
                        selected = select_model(gateway.models(), requested_model)
                    reply = gateway.chat(selected, [{"role": "user", "content": prompt}], max_tokens=limit)
                    result = {"content": [{"type": "text", "text": json.dumps(redact({
                        "text": text_of(message_of(reply)), "requested_model": selected,
                        "provider_reported_model": reply.get("model"), "usage": reply.get("usage")}, gateway.key), ensure_ascii=False)}], "isError": False}
                except KitError as exc:
                    result = {"content": [{"type": "text", "text": json.dumps({"error": exc.code, "message": redact(str(exc), gateway.key)}, ensure_ascii=False)}], "isError": True}
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found."}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        except KitError as exc:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": redact(str(exc), gateway.key)}})


def codex_command(gateway: Gateway, model: str, extra: list[str]) -> list[str]:
    if extra[:1] == ["--"]:
        extra = extra[1:]
    # JSON string literals are also valid TOML basic strings. Never build a shell string.
    q = json.dumps
    return [shutil.which("codex") or "codex", "-m", model,
            "-c", 'model_provider="magpie_kit"',
            "-c", 'model_providers.magpie_kit.name="Gemini Agent Kit gateway"',
            "-c", "model_providers.magpie_kit.base_url=" + q(gateway.base_url),
            "-c", "model_providers.magpie_kit.env_key=" + q(KEY_ENV),
            "-c", 'model_providers.magpie_kit.wire_api="responses"', *extra]


def add_common(parser, suppress=False):
    def default(value): return argparse.SUPPRESS if suppress else value
    parser.add_argument("--base-url", default=default(DEFAULT_URL), help="OpenAI-compatible API base (default: local Magpie /v1)")
    parser.add_argument("--model", default=default(None), help="Exact /models ID; if omitted, select only a unique Gemini Argon entry")
    parser.add_argument("--timeout", type=float, default=default(30.0), help="Per-request timeout in seconds (max 600)")
    parser.add_argument("--allow-insecure-http", action="store_true", default=default(False), help="Explicitly allow remote HTTP on a trusted network")


def main(argv=None):
    # MCP specifies UTF-8. Explicit configuration also keeps Windows pipes from
    # silently decoding prompts using an unrelated legacy console codepage.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    add_common(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "models", "plan", "verify", "ask", "mcp", "launch-codex"):
        sub = subs.add_parser(command)
        add_common(sub, suppress=True)
        if command in ("plan", "verify", "ask"):
            sub.add_argument("--output", required=True, help="New JSON report path; existing files are not overwritten")
        if command == "ask":
            sub.add_argument("--prompt-file", type=Path, required=True, help="UTF-8 task file, at most 64000 characters")
            sub.add_argument("--max-tokens", type=int, default=1024, help="Output token limit, 1 to 8192")
        if command == "plan":
            sub.add_argument("--client", required=True, choices=("codex", "workbuddy", "muse", "grokbot", "doubao", "generic"))
            sub.add_argument("--runtime-script", help="Absolute script path on the connector's own runtime")
        if command == "launch-codex":
            sub.add_argument("--dry-run", action="store_true", help="Print a credential-free argv plan instead of launching")
            sub.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    key = os.environ.get(KEY_ENV, "")
    try:
        if args.command in ("plan", "verify", "ask"):
            check_new_output(args.output)
        if args.command == "ask":
            try:
                with args.prompt_file.open(encoding="utf-8") as prompt_file:
                    prompt = prompt_file.read(64001)
            except UnicodeDecodeError:
                raise KitError("INVALID_PROMPT", "The task file must use UTF-8.") from None
            if not prompt.strip() or len(prompt) > 64000 or not 1 <= args.max_tokens <= 8192:
                raise KitError("INVALID_PROMPT", "Provide a nonempty UTF-8 task of at most 64000 characters and max-tokens from 1 to 8192.")
        gateway = Gateway(args.base_url, args.timeout, args.allow_insecure_http)
        if args.command == "mcp":
            mcp_serve(gateway, args.model)
            return 0
        if args.command == "doctor":
            result = doctor(gateway)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        catalog = gateway.models()
        if args.command == "models":
            print(json.dumps(redact({"gateway": gateway.base_url, "models": catalog}, gateway.key), ensure_ascii=False, indent=2))
            return 0
        model = select_model(catalog, args.model)
        if args.command == "ask":
            reply = gateway.chat(model, [{"role": "user", "content": prompt}], max_tokens=args.max_tokens)
            text = text_of(message_of(reply))
            if not text.strip():
                raise KitError("EMPTY_REPLY", "The gateway returned no text. No output file was saved and no retry was attempted.")
            path = save_json(args.output, {"text": text, "requested_model": model,
                "provider_reported_model": reply.get("model"), "usage": reply.get("usage"),
                "finish_reason": reply["choices"][0].get("finish_reason")}, gateway.key)
            print(json.dumps({"status": "reply_saved", "path": path, "model": model}, ensure_ascii=False))
            return 0
        if args.command == "plan":
            plan = make_plan(gateway, model, args.client, args.runtime_script)
            path = save_json(args.output, plan, gateway.key)
            print(json.dumps({"status": "plan_saved", "path": path, "client": args.client, "model": model}, ensure_ascii=False))
            return 0
        if args.command == "verify":
            report = verify(gateway, model)
            path = save_json(args.output, report, gateway.key)
            print(json.dumps({"passed": report["passed"], "path": path, "scope": report["scope"], "client_end_to_end_verified": False}, ensure_ascii=False))
            return 0 if report["passed"] else 1
        if args.command == "launch-codex":
            cmd = codex_command(gateway, model, args.codex_args)
            if args.dry_run:
                print(json.dumps({"argv": cmd, "credential_environment_variable": KEY_ENV, "global_config_modified": False}, ensure_ascii=False, indent=2))
                return 0
            if shutil.which("codex") is None:
                raise KitError("CODEX_NOT_FOUND", "Install Codex CLI and make codex available on PATH before launching.")
            env = os.environ.copy()
            env[KEY_ENV] = gateway.key
            return subprocess.run(cmd, env=env, check=False).returncode
        return 1
    except KitError as exc:
        print(json.dumps({"error": exc.code, "message": redact(str(exc), key)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError as exc:
        print(json.dumps({"error": "LOCAL_IO_ERROR", "message": redact(str(exc), key)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
