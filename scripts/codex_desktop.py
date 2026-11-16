"""Reversible Codex user-provider configuration; Python 3.11+ only.

This configures a provider. It does NOT verify the desktop model picker, change
an existing conversation, restart Codex, or make a model request. A desktop
restart/new conversation and an actual task remain separate verification steps.

The ``credential`` subcommand is exclusively a Codex credential-helper entry
point: its stdout contains the private *Kit gateway key*. Local agents must not
run it in a terminal/tool that echoes stdout. Preview/apply/restore never read
that key, and neither their returned metadata nor receipts contain credentials.

Only this module owns ``gemini_agent_kit``. Conflicting existing definitions are
rejected rather than silently rewritten. Unknown TOML and comments are preserved
byte-for-byte; activation changes only the two root values. Backups are private.
Hash guards reject edits made after preview or during installation/restoration;
the lock serializes this module's writers (other editors do not share that lock).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit, urlunsplit
import uuid


PROVIDER_ID = "gemini_agent_kit"
BACKUP_DIR = ".gemini-agent-kit-backups"
DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
_UNSET = object()
_MAX_BYTES = 4 * 1024 * 1024


class ConfigError(ValueError):
    """A safe, user-facing configuration error, without file contents/secrets."""


def _path(value) -> Path:
    """Absolute paths, no user-controlled symlink components.

    macOS's built-in /var, /tmp and /etc aliases are canonicalized first so that
    its normal temporary directory works; all remaining symlinks are rejected.
    """
    result = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    if sys.platform == "darwin":
        for alias in ("var", "tmp", "etc"):
            prefix = Path("/") / alias
            if result == prefix or prefix in result.parents:
                if prefix.is_symlink() and os.readlink(prefix) == f"private/{alias}":
                    result = Path("/private") / result.relative_to("/")
                break
    for part in (*reversed(result.parents), result):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ConfigError("Symbolic links are not accepted for configuration or private state paths.")
        if part != result and not stat.S_ISDIR(info.st_mode):
            raise ConfigError("A path parent is not a directory.")
    return result


def _read(path: Path, *, missing=False, private=False) -> bytes | None:
    path = _path(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing:
            return None
        raise ConfigError("A required configuration or private state file is missing.") from None
    except OSError:
        raise ConfigError("Cannot safely open a configuration or private state file.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise ConfigError("Expected a regular configuration file smaller than 4 MiB.")
        if private and os.name == "posix":
            if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise ConfigError("Private state/receipt files must belong to the current user and have mode 0600.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ConfigError("Configuration file exceeds the size limit.")
        return data
    finally:
        os.close(descriptor)


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _parse(data: bytes | None) -> tuple[str, dict]:
    try:
        text = (data or b"").decode("utf-8")
        return text, tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("Codex configuration is not valid UTF-8 TOML; no changes were written.") from None


def _quoted(value: str) -> str:
    # JSON string escaping is valid TOML basic-string escaping for these values.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _definition(state: Path, base_url: str) -> tuple[dict, str]:
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                 and parsed.port is not None and parsed.username is None and parsed.password is None
                 and parsed.path.rstrip("/") == "/v1" and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ConfigError("The Kit endpoint must be a loopback HTTP URL with an explicit port and /v1 path.")
    base_url = urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
    catalog_url = urlunsplit((parsed.scheme, parsed.netloc, "/codex/models", "", ""))
    definition = {
        "name": "Gemini Agent Kit",
        "base_url": base_url,
        "wire_api": "responses",
        "model_catalog_url": catalog_url,
        "supports_websockets": False,
        "auth": {
            "command": str(Path(sys.executable).resolve()),
            "args": [str(Path(__file__).resolve()), "credential", "--state", str(state)],
            "timeout_ms": 5000,
            "refresh_interval_ms": 300000,
        },
    }
    lines = [f"[model_providers.{PROVIDER_ID}]"]
    for key, value in definition.items():
        if key != "auth":
            lines.append(f"{key} = {str(value).lower() if isinstance(value, bool) else _quoted(value)}")
    lines += ["", f"[model_providers.{PROVIDER_ID}.auth]"]
    for key, value in definition["auth"].items():
        encoded = ("[" + ", ".join(_quoted(item) for item in value) + "]" if isinstance(value, list)
                   else _quoted(value) if isinstance(value, str) else str(value))
        lines.append(f"{key} = {encoded}")
    return definition, "\n".join(lines) + "\n"


def _statements(text: str):
    """Yield TOML statement offsets without splitting multiline strings/arrays."""
    start = index = depth = 0
    quote = None
    comment = False
    while index < len(text):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
                if depth == 0:
                    yield start, index + 1
                    start = index + 1
            index += 1
            continue
        if quote:
            if quote[0] == '"' and char == "\\":
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                # A multiline TOML string can terminate in four/five quotes.
                if len(quote) == 3:
                    while index < len(text) and text[index] == quote[0]:
                        index += 1
                quote = None
            else:
                index += 1
            continue
        if char in ("'", '"'):
            quote = char * (3 if text.startswith(char * 3, index) else 1)
            index += len(quote)
            continue
        if char == "#":
            comment = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "\n" and depth == 0:
            yield start, index + 1
            start = index + 1
        index += 1
    if start < len(text):
        yield start, len(text)


def _value_bounds(statement: str) -> tuple[int, int]:
    """Find a parsed assignment's value, retaining its whitespace and comment."""
    quote = None
    depth = index = 0
    begin = None
    end = len(statement)
    while index < len(statement):
        char = statement[index]
        if quote:
            if quote[0] == '"' and char == "\\":
                index += 2
                continue
            if statement.startswith(quote, index):
                index += len(quote)
                if len(quote) == 3:
                    while index < len(statement) and statement[index] == quote[0]:
                        index += 1
                quote = None
            else:
                index += 1
            continue
        if char in ("'", '"'):
            quote = char * (3 if statement.startswith(char * 3, index) else 1)
            index += len(quote)
            continue
        if begin is None and char == "=":
            begin = index + 1
            while begin < len(statement) and statement[begin] in " \t":
                begin += 1
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "#":
            if depth == 0:
                end = index
                break
            newline = statement.find("\n", index)
            index = len(statement) if newline < 0 else newline + 1
            continue
        index += 1
    if begin is None:
        raise ConfigError("Cannot safely locate a root setting in the original TOML.")
    while end > begin and statement[end - 1] in " \t\r\n":
        end -= 1
    return begin, end


def _activate(text: str, parsed: dict, model: str) -> tuple[str, dict]:
    wanted = {"model": model, "model_provider": PROVIDER_ID}
    changes = {key: value for key, value in wanted.items() if parsed.get(key) != value}
    edits = []
    located = set()
    for begin, end in _statements(text):
        statement = text[begin:end]
        clean = statement.lstrip()
        if clean.startswith("["):
            break
        if not clean or clean.startswith("#"):
            continue
        try:
            standalone = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            raise ConfigError("Cannot safely inspect a root TOML statement.") from None
        for key, value in changes.items():
            if key in standalone:
                if len(standalone) != 1 or isinstance(standalone[key], dict):
                    raise ConfigError("A root model setting has an unsupported dotted/table form.")
                a, b = _value_bounds(statement)
                edits.append((begin + a, begin + b, _quoted(value)))
                located.add(key)
    for a, b, value in sorted(edits, reverse=True):
        text = text[:a] + value + text[b:]
    newline = "\r\n" if "\r\n" in text else "\n"
    added = []
    for key, value in changes.items():
        if key not in located:
            if key in parsed:
                raise ConfigError("A root model setting cannot be changed without rewriting unknown TOML.")
            added.append(f"{key} = {_quoted(value)}{newline}")
    return "".join(added) + text, changes


def _plan(config_path, state_path, base_url, model, activate):
    config, state = _path(config_path), _path(state_path)
    if not isinstance(model, str) or not model or len(model) > 512 or any(ord(c) < 32 for c in model):
        raise ConfigError("Choose a nonempty model ID from the gateway catalog.")
    if type(activate) is not bool:
        raise ConfigError("activate must be a boolean.")
    if config == state:
        raise ConfigError("The Codex configuration and Kit private state must be different files.")
    # Only inspect the state path: preview/install never read its credential.
    if state.exists() and not stat.S_ISREG(state.lstat().st_mode):
        raise ConfigError("Kit state must be a regular file.")
    before = _read(config, missing=True)
    original, parsed = _parse(before)
    definition, block = _definition(state, base_url)
    providers = parsed.get("model_providers", {})
    if not isinstance(providers, dict):
        raise ConfigError("model_providers must be a TOML table.")
    existing = providers.get(PROVIDER_ID)
    added = existing is None
    if not added:
        if (not isinstance(existing, dict)
                or any(existing.get(k) != v for k, v in definition.items())
                or any(k in existing for k in ("env_key", "experimental_bearer_token", "requires_openai_auth"))):
            raise ConfigError("The gemini_agent_kit provider already exists with different settings; restore or edit it explicitly first.")
    after_text, roots = _activate(original, parsed, model) if activate else (original, {})
    if added:
        newline = "\r\n" if "\r\n" in original else "\n"
        if after_text and not after_text.endswith("\n"):
            after_text += newline
        if after_text:
            after_text += newline
        after_text += block.replace("\n", newline)
    after = after_text.encode("utf-8")
    try:
        _, validated = _parse(after)
    except ConfigError:
        raise ConfigError("The existing TOML layout cannot be extended safely without rewriting it; no changes were written.") from None
    if validated.get("model_providers", {}).get(PROVIDER_ID) != (definition if added else existing):
        raise ConfigError("Provider validation failed; no changes were written.")
    for key, value in roots.items():
        if validated.get(key) != value:
            raise ConfigError("Root model validation failed; no changes were written.")
    # Review projection intentionally excludes all unrelated settings/comments
    # and the previous values: even an existing inline API key cannot leak.
    review_before, review_after = [], []
    for key, value in roots.items():
        if key in parsed:
            review_before.append(f"{key} = <previous value omitted>\n")
        review_after.append(f"{key} = {_quoted(value)}\n")
    if added:
        review_after.extend(block.splitlines(keepends=True))
    result = {
        "provider_id": PROVIDER_ID,
        "config_path": str(config),
        "state_path": str(state),
        "base_url": definition["base_url"],
        "model_catalog_url": definition["model_catalog_url"],
        "model": model,
        "activate": activate,
        "changes": before != after,
        "provider_added": added,
        "default_changed": bool(roots),
        "before_sha256": _sha(before),
        "after_sha256": _sha(after),
        "diff": "".join(difflib.unified_diff(review_before, review_after, fromfile="managed settings (before)", tofile="managed settings (proposed)", n=0)),
        "diff_scope": "Only managed settings are shown; previous values and unrelated content are omitted. Original comments/content are preserved on disk.",
        "desktop_verified": False,
        "restart_required": before != after,
        "warnings": (["An existing global model_catalog_json may override this provider's remote catalog; it was preserved."]
                     if parsed.get("model_catalog_json") else []),
    }
    return result, before, after


def preview(config_path, state_path, base_url=DEFAULT_BASE_URL, model="gemini-4-argon-high", activate=False) -> dict:
    """Read-only review. Pass before_sha256 to apply to guard this exact review."""
    return _plan(config_path, state_path, base_url, model, activate)[0]


def _private_dir(directory: Path):
    directory = _path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ConfigError("The private backup location is not a directory.")
    os.chmod(directory, 0o700)


@contextmanager
def _lock(config: Path):
    directory = config.parent / BACKUP_DIR
    _private_dir(directory)
    lock = _path(directory / f"{config.name}.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise ConfigError("Another configuration operation holds the lock; wait for it to finish.") from None
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield directory
    finally:
        os.close(descriptor)
        lock.unlink()


def _write_new(path: Path, data: bytes):
    path = _path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_if_unchanged(path: Path, data: bytes, expected_hash, mode=0o600):
    """Atomic file replacement with a final optimistic concurrency check."""
    path = _path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.kit-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
        if _sha(_read(path, missing=True)) != expected_hash:
            raise ConfigError("Configuration changed concurrently; refusing to overwrite it.")
        _path(path)
        os.replace(temporary, path)
    finally:
        os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _receipt_bytes(receipt):
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def apply(config_path, state_path, base_url=DEFAULT_BASE_URL, model="gemini-4-argon-high", activate=False,
          *, expected_before_sha256=_UNSET) -> dict:
    """Install with private backup/receipt; default behavior keeps current model.

    Supply preview()['before_sha256'] (including None for an absent file) as
    expected_before_sha256 to ensure the reviewed configuration has not changed.
    """
    config = _path(config_path)
    with _lock(config) as directory:
        plan, before, after = _plan(config, state_path, base_url, model, activate)
        if expected_before_sha256 is not _UNSET and expected_before_sha256 != plan["before_sha256"]:
            raise ConfigError("Configuration changed after preview; review it again before saving.")
        if not plan["changes"]:
            return {"changed": False, "receipt_path": None, "provider_id": PROVIDER_ID,
                    "config_path": str(config), "desktop_verified": False, "restart_required": False}
        identifier = uuid.uuid4().hex
        backup = directory / f"{identifier}.before.toml" if before is not None else None
        receipt_path = directory / f"{identifier}.receipt.json"
        before_mode = stat.S_IMODE(config.stat().st_mode) if before is not None else None
        if backup is not None:
            _write_new(backup, before)
        receipt = {
            "schema_version": 1, "operation": "codex_provider_configuration", "provider_id": PROVIDER_ID,
            "config_path": str(config), "receipt_path": str(receipt_path),
            "backup_file": backup.name if backup else None,
            "before_sha256": plan["before_sha256"], "after_sha256": plan["after_sha256"],
            "before_mode": before_mode, "activate": activate, "changed": True,
            "desktop_verified": False, "restart_required": True, "status": "prepared",
        }
        _write_new(receipt_path, _receipt_bytes(receipt))
        _replace_if_unchanged(config, after, plan["before_sha256"])
        prior_receipt_hash = _sha(_receipt_bytes(receipt))
        receipt["status"] = "applied"
        _replace_if_unchanged(receipt_path, _receipt_bytes(receipt), prior_receipt_hash)
        return receipt


def restore(config_path, receipt_path) -> dict:
    """Restore exact prior bytes only if the installed file has not been edited."""
    config, source = _path(config_path), _path(receipt_path)
    if source.parent != config.parent / BACKUP_DIR:
        raise ConfigError("Use the private receipt saved alongside this Codex configuration.")
    with _lock(config) as directory:
        raw = _read(source, private=True)
        try:
            receipt = json.loads(raw)
            valid = (receipt["schema_version"] == 1 and receipt["provider_id"] == PROVIDER_ID
                     and receipt["config_path"] == str(config) and receipt["receipt_path"] == str(source)
                     and receipt["status"] in ("prepared", "applied", "restored")
                     and re.fullmatch(r"[0-9a-f]{64}", receipt["after_sha256"])
                     and (receipt["before_sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", receipt["before_sha256"])))
            if not valid:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ConfigError("The restoration receipt is invalid.") from None
        current = _sha(_read(config, missing=True))
        if current == receipt["before_sha256"]:
            return {"restored": True, "changed": False, "config_path": str(config), "desktop_verified": False}
        if current != receipt["after_sha256"]:
            raise ConfigError("Codex configuration was edited after installation; refusing to overwrite those changes.")
        if receipt["before_sha256"] is None:
            if receipt.get("backup_file") is not None:
                raise ConfigError("The restoration receipt has inconsistent backup metadata.")
            if _sha(_read(config)) != current:
                raise ConfigError("Configuration changed concurrently; refusing to remove it.")
            _path(config).unlink()
        else:
            name = receipt.get("backup_file")
            if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}\.before\.toml", name):
                raise ConfigError("The restoration backup name is invalid.")
            backup = _read(directory / name, private=True)
            if _sha(backup) != receipt["before_sha256"]:
                raise ConfigError("Backup checksum mismatch; refusing to restore it.")
            _parse(backup)
            mode = receipt.get("before_mode")
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise ConfigError("The restoration file mode is invalid.")
            _replace_if_unchanged(config, backup, current, mode)
        receipt["status"] = "restored"
        _replace_if_unchanged(source, _receipt_bytes(receipt), _sha(raw))
        return {"restored": True, "changed": True, "config_path": str(config),
                "receipt_path": str(source), "desktop_verified": False, "restart_required": True}


def _credential(state_path) -> str:
    """Private helper implementation. Never include its result in logs/reports."""
    try:
        state = json.loads(_read(_path(state_path), private=True))
        key = state["gateway_key"]
        if not isinstance(key, str) or not key or key != key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError
        return key
    except (ConfigError, ValueError, TypeError, KeyError):
        raise ConfigError("Kit gateway credential is unavailable; check the local Kit state and its private permissions.") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    credential = sub.add_parser("credential", help="Codex-only credential helper; stdout is a secret")
    credential.add_argument("--state", required=True)
    for name in ("preview", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
        command.add_argument("--state", required=True)
        command.add_argument("--base-url", default=DEFAULT_BASE_URL)
        command.add_argument("--model", required=True)
        command.add_argument("--activate", action="store_true")
        if name == "apply":
            command.add_argument("--expected-before-sha256", help="Use 'missing' for an absent config")
    rollback = sub.add_parser("restore")
    rollback.add_argument("--config", required=True)
    rollback.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "credential":
            sys.stdout.write(_credential(args.state))
            return 0
        if args.action == "restore":
            result = restore(args.config, args.receipt)
        else:
            kwargs = {}
            if args.action == "apply" and args.expected_before_sha256 is not None:
                kwargs["expected_before_sha256"] = None if args.expected_before_sha256 == "missing" else args.expected_before_sha256
            result = globals()[args.action](args.config, args.state, args.base_url, args.model, args.activate, **kwargs)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, OSError):
        # Error objects from OS/JSON/TOML are intentionally not echoed by the
        # helper. Other commands use the same conservative CLI error boundary.
        print("Codex provider operation failed; check paths, permissions, conflicts and configuration hashes.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
