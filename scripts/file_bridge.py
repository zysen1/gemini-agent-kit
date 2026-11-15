#!/usr/bin/env python3
"""Bounded file relay for a cloud agent with authorized local-file access.

No listening port, shell command, caller-selected URL, or caller-selected model.
Python 3.10+, standard library only. Credentials come only from the environment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid

from agent_kit import KEY_ENV, KitError, NoRedirect, normalize_url, redact

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def scrub(value, key=""):
    value = redact(value, key)
    if isinstance(value, dict):
        return {k: scrub(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        return re.sub(r"1//[A-Za-z0-9_-]+", "[REDACTED_REFRESH_TOKEN]", value)
    return value


def request_document(raw: bytes, request_id: str, key: str = "") -> dict:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Request exceeds 64 KiB.")
    obj = json.loads(raw)
    if not isinstance(obj, dict) or set(obj) != {"id", "prompt", "max_tokens"}:
        raise ValueError("Expected exactly id, prompt and max_tokens fields.")
    if not isinstance(obj["id"], str) or not ID_RE.fullmatch(obj["id"]) or obj["id"] != request_id:
        raise ValueError("Request id must match its safe filename.")
    prompt = obj["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ValueError("Prompt must contain 1–16000 characters.")
    if (key and key in prompt) or re.search(r"1//[A-Za-z0-9_-]{12,}", prompt):
        raise ValueError("Do not put gateway keys or refresh tokens in requests.")
    tokens = obj["max_tokens"]
    if type(tokens) is not int or not 1 <= tokens <= 4096:
        raise ValueError("max_tokens must be an integer from 1 to 4096.")
    return obj


def cooldown_seconds(headers, body: str) -> float:
    delay = 0.0
    value = headers.get("Retry-After", "") if headers else ""
    try:
        delay = max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            deadline = email.utils.parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=dt.timezone.utc)
            delay = max(0.0, deadline.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    for match in re.finditer(r"(?:cooldown(?:\s+wait)?|retry\s+after|wait)[\s:=]+(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)", body, re.I):
        delay = max(delay, float(match.group(1)))
    # Google RPC RetryInfo commonly carries retryDelay: "120s" without a header.
    for match in re.finditer(r'"(?:retryDelay|retry_delay)"\s*:\s*"(\d+(?:\.\d+)?)s"', body):
        delay = max(delay, float(match.group(1)))
    return delay if math.isfinite(delay) else 0.0


class HTTPGateway:
    def __init__(self, base_url, model, key, timeout=180):
        self.base_url, local = normalize_url(base_url)
        if not local:
            raise ValueError("This relay accepts only a loopback gateway.")
        if not key or any(c in key for c in "\r\n"):
            raise ValueError(f"Set a nonempty {KEY_ENV} in the worker environment.")
        if not model or len(model) > 200 or any(ord(c) < 33 for c in model):
            raise ValueError("Provide the exact model ID using --model.")
        if not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise ValueError("Timeout must be 1–600 seconds.")
        self.model, self.key, self.timeout = model, key, timeout
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def call(self, document):
        payload = {"model": self.model, "messages": [{"role": "user", "content": document["prompt"]}],
                   "stream": False, "max_tokens": document["max_tokens"]}
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status, headers = response.status, response.headers
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"success": False, "http_status": status, "error": "RESPONSE_TOO_LARGE"}, 0
            obj = json.loads(raw)
            if not isinstance(obj, dict) or "error" in obj:
                detail = json.dumps(obj, ensure_ascii=False)[:8192]
                delay = cooldown_seconds(headers, detail)
                return {"success": False, "http_status": status, "error": "UPSTREAM_ERROR", "detail": scrub(obj, self.key),
                        "retry_after_seconds": delay}, delay
            choices = obj.get("choices")
            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                return {"success": False, "http_status": status, "error": "NO_TEXT_RESPONSE"}, 0
            return scrub({"success": True, "http_status": status, "text": content,
                          "provider_reported_model": obj.get("model"),
                          "provider_model_matches_request": obj.get("model") == self.model,
                          "route_headers": {k: headers.get(k) for k in ("X-Mapped-Model", "X-Upstream-Model")},
                          "usage": obj.get("usage"), "finish_reason": choices[0].get("finish_reason")}, self.key), 0
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(8192).decode("utf-8", "replace")
                delay = cooldown_seconds(exc.headers, body) if exc.code in (429, 503) else 0
                return scrub({"success": False, "http_status": exc.code, "error": "HTTP_ERROR", "detail": body[:2500],
                              "retry_after_seconds": delay}, self.key), delay
            finally:
                exc.close()
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return scrub({"success": False, "error": "TRANSPORT_OR_RESPONSE_ERROR", "detail": str(exc)[:1200]}, self.key), 0


def private_directory(path: Path):
    if path.is_symlink():
        raise ValueError("Queue directories must not be symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Queue path must be a directory.")


def read_regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Request must be a regular file, not a symbolic link.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
            raise ValueError("Request changed while opening.")
        if opened.st_size > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds 64 KiB.")
        return stream.read(MAX_REQUEST_BYTES + 1)


def exclusive_json(path: Path, value):
    """Publish a complete JSON file atomically without replacing any existing file."""
    temp = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(temp, path)  # Atomic no-clobber; unlike os.replace, preserves existing results.
    finally:
        temp.unlink(missing_ok=True)


class BoundDirectory:
    """POSIX dirfd operations keep a replaced path from redirecting file I/O."""
    def __init__(self, path):
        private_directory(path)
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)

    def verify(self):
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
            raise ValueError("Queue directory identity changed; worker stopped.")

    def read(self, name):
        self.verify()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
                raise ValueError("Request must be a regular file no larger than 64 KiB.")
            return stream.read(MAX_REQUEST_BYTES + 1)

    def exists(self, name):
        self.verify()
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def publish(self, name, value, replace=False):
        self.verify()
        temp = "." + name + "." + uuid.uuid4().hex + ".tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            self.verify()
            if replace:
                os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                os.link(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def claim(self, name, destination):
        self.verify(); destination.verify()
        os.link(name, name, src_dir_fd=self.fd, dst_dir_fd=destination.fd, follow_symlinks=False)
        os.unlink(name, dir_fd=self.fd)


class Worker:
    def __init__(self, queue: Path, gateway, min_interval=35, stable_seconds=1, receipt_dir=None):
        if os.name != "posix":
            raise ValueError("The file relay currently requires macOS or Linux directory-descriptor support.")
        if not queue.is_absolute():
            raise ValueError("--queue must be an absolute path.")
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("Minimum interval must be finite and nonnegative.")
        self.queue, self.gateway = queue, gateway
        self.interval, self.stable_seconds = min_interval, stable_seconds
        receipt_dir = receipt_dir or queue.parent / (queue.name + "-private-receipts")
        if not receipt_dir.is_absolute() or receipt_dir.resolve().is_relative_to(queue.resolve()):
            raise ValueError("Receipts must use a separate absolute directory outside the shared queue.")
        self.directories = {name: BoundDirectory(path) for name, path in (
            ("queue", queue), ("requests", queue / "requests"), ("results", queue / "results"),
            ("processed", queue / "processed"), ("receipts", receipt_dir))}
        self.state_path = receipt_dir / ".cooldown.json"
        self.next_request_at = 0.0
        if self.state_path.exists() or self.state_path.is_symlink():
            state_obj = json.loads(self.directories["receipts"].read(".cooldown.json"))
            deadline = state_obj.get("next_request_at")
            if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline < 0:
                raise ValueError("Invalid persisted cooldown; inspect it before restarting.")
            self.next_request_at = deadline
        self.seen = {}

    def _persist_cooldown(self):
        self.directories["receipts"].publish(".cooldown.json", {"next_request_at": self.next_request_at}, replace=True)

    def _ready(self, path, immediate):
        info = path.lstat()
        stamp = (info.st_size, info.st_mtime_ns)
        prior = self.seen.get(path.name)
        if prior is None or prior[0] != stamp:
            self.seen[path.name] = (stamp, time.monotonic())
            return immediate or self.stable_seconds == 0
        return time.monotonic() - prior[1] >= self.stable_seconds

    def step(self, immediate=False):
        for directory in self.directories.values():
            directory.verify()
        if time.time() < self.next_request_at:
            return False
        for filename in sorted(os.listdir(self.directories["requests"].fd)):
            if not filename.endswith(".json"):
                continue
            path = self.queue / "requests" / filename
            request_id = path.stem
            if not ID_RE.fullmatch(request_id):
                continue  # Never derive output paths from an unsafe filename.
            result_path = self.queue / "results" / path.name
            processed_path = self.queue / "processed" / path.name
            private_claim_name = ".claim-" + path.name
            if (self.directories["results"].exists(path.name) or self.directories["processed"].exists(path.name)
                    or self.directories["receipts"].exists(private_claim_name)) :
                continue  # IDs are immutable, including across worker restarts.
            try:
                if not self._ready(path, immediate):
                    continue
                raw = self.directories["requests"].read(path.name)
                document = request_document(raw, request_id, self.gateway.key)
            except (FileNotFoundError, PermissionError):
                continue
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A cloud writer may expose a partial file. Wait until it is unchanged
                # for 10 seconds; --once reports incomplete files as unprocessed.
                if immediate or time.monotonic() - self.seen[path.name][1] < 10:
                    continue
                raw = b""; document = None; validation_error = "INVALID_JSON"
            except (ValueError, OSError) as exc:
                raw = b""; document = None; validation_error = str(exc)
            # Claim by hard link, then unlink: no overwrite and only one worker wins.
            # A symlink input is rejected above; it is removed from the queue but never read.
            try:
                self.directories["receipts"].publish(private_claim_name, {
                    "id": request_id, "claimed_at_utc": now(), "requested_model": self.gateway.model,
                    "request_sha256": hashlib.sha256(raw).hexdigest() if raw else None})
                self.directories["requests"].claim(path.name, self.directories["processed"])
            except (FileExistsError, FileNotFoundError):
                continue
            record = {"id": request_id, "started_at_utc": now(), "requested_model": self.gateway.model,
                      "gateway": self.gateway.base_url, "protocol": "OpenAI Chat Completions via local file relay",
                      "model_identity_independently_verified": False, "inference_request_count": 0,
                      "request_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
                      "automatic_retries": 0}
            started = time.monotonic()
            delay = 0
            if document is None:
                record.update(success=False, error="INVALID_REQUEST", detail=validation_error)
            else:
                # Check the claimed bytes again in case the producer changed them.
                try:
                    claimed = self.directories["processed"].read(path.name)
                    if claimed != raw:
                        raise ValueError("Request changed while being claimed; use a new id.")
                    record["inference_request_count"] = 1
                    result, delay = self.gateway.call(document)
                    record.update(result)
                except (ValueError, OSError) as exc:
                    record.update(success=False, error="REQUEST_CHANGED", detail=str(exc))
                self.next_request_at = time.time() + max(self.interval, delay)
                self._persist_cooldown()
            record.update(finished_at_utc=now(), elapsed_seconds=round(time.monotonic() - started, 3),
                          next_request_not_before_utc=dt.datetime.fromtimestamp(self.next_request_at, dt.timezone.utc).isoformat() if self.next_request_at else None)
            record = scrub(record, self.gateway.key)
            # A shared result can be forged by a writer. Keep a separate local receipt;
            # only this receipt plus matching result hash supports the execution record.
            record["receipt_id"] = uuid.uuid4().hex
            result_hash = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            receipt = {key: record.get(key) for key in ("id", "receipt_id", "started_at_utc", "finished_at_utc",
                       "success", "http_status", "error", "requested_model", "provider_reported_model", "route_headers",
                       "request_sha256", "inference_request_count")}
            receipt["result_canonical_sha256"] = result_hash
            self.directories["receipts"].publish(path.name, receipt)
            self.directories["results"].publish(path.name, record)
            print(json.dumps({"id": request_id, "success": record["success"], "inference_request_count": record["inference_request_count"],
                              "result": str(result_path), "error": record.get("error")}), flush=True)
            return True
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True, help="Private local receipt directory outside the shared queue.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8045/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--min-interval", type=float, default=35)
    parser.add_argument("--once", action="store_true", help="Process at most one complete request, then exit.")
    args = parser.parse_args(argv)
    lock = None
    try:
        gateway = HTTPGateway(args.base_url, args.model, os.environ.get(KEY_ENV, "").strip(), args.timeout)
        worker = Worker(args.queue, gateway, args.min_interval, receipt_dir=args.receipt_dir)
        lock = args.receipt_dir / ".worker.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            lock = None
            if isinstance(exc, FileExistsError):
                raise ValueError("A worker lock already exists. Check the recorded PID; remove a stale lock only after confirming the worker stopped.") from None
            raise
        with os.fdopen(fd, "w") as stream:
            json.dump({"pid": os.getpid(), "started_at_utc": now()}, stream)
        print(json.dumps({"worker": "ready", "queue": str(args.queue), "model": args.model,
                          "gateway": gateway.base_url, "min_interval_seconds": args.min_interval,
                          "automatic_retries": 0}), flush=True)
        if args.once:
            return 0 if worker.step(immediate=True) else 2
        while True:
            worker.step()
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except (ValueError, KitError, OSError) as exc:
        print(json.dumps({"error": scrub(str(exc), os.environ.get(KEY_ENV, ""))}), file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
