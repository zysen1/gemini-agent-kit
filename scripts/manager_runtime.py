"""Run an isolated, unmodified Antigravity Tools v5.0.3 backend.

The backend is a separate upstream work under CC BY-NC-SA 4.0, not bundled
or relicensed by this project. setup() never executes downloaded software.
Only start() launches the backend; no method sends model-generation requests.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


VERSION = "5.0.3"
RELEASE_URL = "https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v5.0.3"
LICENSE_URL = "https://github.com/lbjlaq/Antigravity-Manager/blob/v5.0.3/LICENSE"
DOWNLOAD_ROOT = "https://github.com/lbjlaq/Antigravity-Manager/releases/download/v5.0.3/"
ASSETS = {
    ("Darwin", "arm64"): (
        "Antigravity.Tools_aarch64.app.tar.gz",
        "5417ad76dff21dcfe867488fa598275be919518d55a861a87a8236ad00da8bbf"),
    ("Darwin", "x86_64"): (
        "Antigravity.Tools_x64.app.tar.gz",
        "cd06b1e660536ef4b0ea70c41928fcc1efdf442f3bafde1be0920f8b188bf4dd"),
    ("Linux", "arm64"): (
        "Antigravity.Tools_5.0.3_aarch64.AppImage",
        "5a16bd5b14f4bbc70713760629d92e3797ab0e99a9eb077bb6db01c09c412b2c"),
    ("Linux", "x86_64"): (
        "Antigravity.Tools_5.0.3_amd64.AppImage",
        "9a53089c97375e636e426a1f6aeb9b047a09e43b20b9c9a4736b67b9b36b076f"),
}
WINDOWS_INSTALLER = DOWNLOAD_ROOT + "Antigravity.Tools_5.0.3_x64-setup.exe"
MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_EXTRACT = 600 * 1024 * 1024


class RuntimeErrorDetail(RuntimeError):
    """An intentionally credential-free error suitable for the local GUI."""


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeErrorDetail("运行目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise RuntimeErrorDetail("运行目录不可用。")
    if os.name != "nt":
        path.chmod(0o700)


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeErrorDetail("组件配置文件无效。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeErrorDetail("组件配置无法读取。") from None
    if not isinstance(value, dict):
        raise RuntimeErrorDetail("组件配置格式无效。")
    return value


def _write_private(path: Path, value: dict) -> None:
    _private_dir(path.parent)
    if path.is_symlink():
        raise RuntimeErrorDetail("组件配置不能是符号链接。")
    descriptor, temporary = tempfile.mkstemp(prefix=".kit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _release_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        permitted = (parsed.scheme == "https" and
                     parsed.hostname in {"github.com", "release-assets.githubusercontent.com"} and
                     parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        permitted = False
    if not permitted:
        raise RuntimeErrorDetail("下载地址或重定向不属于允许的 GitHub 发布域名。")
    return url


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _release_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeErrorDetail("本机管理接口不允许重定向。")


def _download(url: str, destination: Path, expected: str) -> None:
    """No account credentials are attached to this request or its redirects."""
    _release_url(url)
    opener = urllib.request.build_opener(_ReleaseRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "gemini-agent-kit"})
    digest = hashlib.sha256()
    started = time.monotonic()
    total = 0
    created = False
    try:
        with opener.open(request, timeout=30) as response:
            _release_url(response.geturl())
            with destination.open("xb") as target:
                created = True
                if os.name != "nt":
                    destination.chmod(0o600)
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_DOWNLOAD or time.monotonic() - started > 300:
                        raise RuntimeErrorDetail("官方下载超过大小或时间限制。")
                    target.write(block)
                    digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeErrorDetail("官方下载 SHA256 不匹配，文件未安装。")
    except Exception as error:
        if created:
            destination.unlink(missing_ok=True)
        if isinstance(error, RuntimeErrorDetail):
            raise
        raise RuntimeErrorDetail("官方下载失败；请检查网络后重试。") from None


def _extract_tar(archive: Path, destination: Path) -> None:
    """Extract only ordinary files/directories, never archive-provided links."""
    _private_dir(destination)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names = set()
            total = 0
            if len(members) > 20000:
                raise RuntimeErrorDetail("安装归档包含过多文件。")
            for member in members:
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or "\\" in member.name or
                        ":" in member.name or not name.parts or str(name) == "." or
                        member.name in names or not (member.isdir() or member.isfile())):
                    raise RuntimeErrorDetail("安装归档包含不安全路径或链接。")
                names.add(member.name)
                total += member.size
                if total > MAX_EXTRACT:
                    raise RuntimeErrorDetail("安装归档解包大小超限。")
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    _private_dir(target)
                    continue
                _private_dir(target.parent)
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeErrorDetail("安装归档文件无法读取。")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o700 if member.mode & 0o100 else 0o600)
    except (OSError, tarfile.TarError):
        raise RuntimeErrorDetail("安装归档无法安全解包。") from None


def _mac_binary(app: Path) -> Path | None:
    info = app / "Contents" / "Info.plist"
    try:
        if info.is_symlink() or info.stat().st_size > 1024 * 1024:
            return None
        data = plistlib.loads(info.read_bytes())
        executable = data.get("CFBundleExecutable", "")
        if (data.get("CFBundleShortVersionString") != VERSION or not executable or
                Path(executable).name != executable):
            return None
        binary = app / "Contents" / "MacOS" / executable
        if binary.is_file() and not binary.is_symlink():
            return binary
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return None


def _windows_binary() -> Path | None:
    """Use an installed version identified by its uninstall metadata; never run installers."""
    if os.name != "nt":
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for branch in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, branch) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                if (winreg.QueryValueEx(key, "DisplayName")[0] != "Antigravity Tools" or
                                        winreg.QueryValueEx(key, "DisplayVersion")[0] != VERSION):
                                    continue
                                location = Path(winreg.QueryValueEx(key, "InstallLocation")[0])
                                if not location.is_absolute():
                                    continue
                                for name in ("antigravity-tools.exe", "Antigravity Tools.exe"):
                                    candidate = location / name
                                    if candidate.is_file() and not candidate.is_symlink():
                                        return candidate
                        except OSError:
                            continue
            except OSError:
                continue
    return None


class ManagerRuntime:
    """Lifecycle owner; credentials are backend-only attributes, never in describe()."""

    def __init__(self, data_dir: str | Path, port: int = 8879):
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Manager 端口必须为 1024–65535。")
        self.data_dir = Path(data_dir).expanduser().absolute()
        self.runtime_dir = self.data_dir / "manager-runtime"
        self.sidecar_dir = self.data_dir / "manager-data"
        self.config_path = self.sidecar_dir / "gui_config.json"
        self.port = port
        self.gateway_key = ""
        self.admin_key = ""
        self._process = None
        self._binary = None
        self._stopped = False
        self._lock = threading.RLock()
        self._load_credentials()

    def _directories(self):
        _private_dir(self.data_dir)
        _private_dir(self.runtime_dir)
        _private_dir(self.sidecar_dir)

    def _load_credentials(self):
        for path in (self.data_dir, self.sidecar_dir):
            if path.is_symlink():
                raise RuntimeErrorDetail("运行目录不能是符号链接。")
        if self.config_path.exists() or self.config_path.is_symlink():
            proxy = _read_json(self.config_path).get("proxy", {})
            if not isinstance(proxy, dict):
                raise RuntimeErrorDetail("组件反代配置格式无效。")
            for attr, name in (("gateway_key", "api_key"), ("admin_key", "admin_password")):
                value = proxy.get(name, "")
                if isinstance(value, str):
                    setattr(self, attr, value)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def describe(self) -> dict:
        alive = self._process is not None and self._process.poll() is None
        status = "running" if alive else ("stopped" if self._stopped or self._process is not None else
                                         "installed" if self._binary else "missing")
        result = {"status": status, "version": VERSION, "managed": True,
                  "base_url": self.base_url, "release_url": RELEASE_URL,
                  "license": "CC BY-NC-SA 4.0", "license_url": LICENSE_URL}
        if status == "missing" and platform.system() == "Windows":
            result.update(download_url=WINDOWS_INSTALLER,
                          instructions="从官方 Release 安装 Antigravity Tools 5.0.3，然后重新点击准备组件；不会自动运行安装器。")
        return result

    def _cached_binary(self) -> Path | None:
        folder = self.runtime_dir / VERSION
        if folder.is_symlink():
            raise RuntimeErrorDetail("组件安装目录不能是符号链接。")
        system = platform.system()
        if system == "Darwin" and folder.exists():
            for app in folder.glob("*.app"):
                binary = _mac_binary(app)
                if binary:
                    return binary
        if system == "Linux":
            candidate = folder / "Antigravity.Tools.AppImage"
            if candidate.is_file() and not candidate.is_symlink():
                machine = self._architecture()
                expected = ASSETS.get((system, machine), ("", ""))[1]
                if hashlib.sha256(candidate.read_bytes()).hexdigest() == expected:
                    return candidate
        return None

    @staticmethod
    def _architecture() -> str:
        value = platform.machine().lower()
        return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(value, value)

    def setup(self) -> dict:
        with self._lock:
            self._directories()
            if self._process is not None and self._process.poll() is None:
                return self.describe()
            self._binary = self._cached_binary()
            if self._binary:
                return self.describe()
            system = platform.system()
            if system == "Darwin":
                for parent in (Path("/Applications"), Path.home() / "Applications"):
                    for name in ("Antigravity Tools.app", "Antigravity.Tools.app"):
                        self._binary = _mac_binary(parent / name)
                        if self._binary:
                            return self.describe()
            if system == "Windows":
                self._binary = _windows_binary()
                return self.describe()
            asset = ASSETS.get((system, self._architecture()))
            if asset is None:
                raise RuntimeErrorDetail("此平台没有已核验的组件发布包，请使用已安装的 Manager 连接方式。")
            name, digest = asset
            target = self.runtime_dir / VERSION
            if target.exists():
                raise RuntimeErrorDetail("已有安装目录未通过检查；保留原文件，请检查后重试。")
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.runtime_dir) as temporary:
                temporary = Path(temporary)
                archive = temporary / name
                _download(DOWNLOAD_ROOT + name, archive, digest)
                unpacked = temporary / "unpacked"
                if system == "Darwin":
                    _extract_tar(archive, unpacked)
                    candidates = [binary for app in unpacked.glob("*.app")
                                  if (binary := _mac_binary(app)) is not None]
                    if len(candidates) != 1:
                        raise RuntimeErrorDetail("官方归档缺少唯一的 5.0.3 应用。")
                    relative = candidates[0].relative_to(unpacked)
                else:
                    _private_dir(unpacked)
                    relative = Path("Antigravity.Tools.AppImage")
                    archive.rename(unpacked / relative)
                    (unpacked / relative).chmod(0o700)
                unpacked.rename(target)
                self._binary = target / relative
            return self.describe()

    def _prepare_config(self):
        self._directories()
        self._load_credentials()
        self.gateway_key = self.gateway_key or "sk-" + secrets.token_urlsafe(32)
        self.admin_key = self.admin_key or secrets.token_urlsafe(48)
        if self.gateway_key == self.admin_key:
            self.admin_key = secrets.token_urlsafe(48)
        config = _read_json(self.config_path) if self.config_path.exists() else {
            "language": "zh", "theme": "system", "auto_refresh": False,
            "refresh_interval": 10, "auto_sync": False, "sync_interval": 5,
        }
        proxy = config.setdefault("proxy", {})
        if not isinstance(proxy, dict):
            raise RuntimeErrorDetail("组件反代配置格式无效。")
        proxy.update(enabled=True, allow_lan_access=False, port=self.port,
                     auth_mode="all_except_health", api_key=self.gateway_key,
                     admin_password=self.admin_key, auto_start=True, enable_logging=False)
        config["auto_refresh"] = False
        config["auto_sync"] = False
        config["scheduled_warmup"] = {"enabled": False, "monitored_models": []}
        _write_private(self.config_path, config)

    def _probe(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        # An empty installation intentionally pauses upstream AI proxy routes.
        # Its authenticated management API must remain ready for account import;
        # /v1/version would return 503 until the proxy is enabled.
        for route in ("/api/proxy/status", "/api/health"):
            request = urllib.request.Request(self.base_url + route,
                                            headers={"Authorization": "Bearer " + self.admin_key})
            with opener.open(request, timeout=1) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeErrorDetail("组件状态响应超限。")
            value = json.loads(raw)
            if not isinstance(value, dict):
                return False
            if route.endswith("status") and value.get("port") != self.port:
                return False
            if route.endswith("health"):
                if value.get("version") != VERSION:
                    raise RuntimeErrorDetail("运行中的组件版本不是固定的 5.0.3。")
                if value.get("status") != "ok":
                    return False
        return True

    def start(self, timeout: float = 20) -> dict:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self.describe()
            if self._binary is None:
                self._binary = self._cached_binary()
            if self._binary is None or not self._binary.is_file():
                raise RuntimeErrorDetail("请先点击准备组件；Windows 需要先完成官方安装。")
            if platform.system() == "Darwin":
                app = self._binary.parent.parent.parent
                if _mac_binary(app) != self._binary:
                    raise RuntimeErrorDetail("已安装组件版本发生变化，请重新准备固定版本。")
            try:
                with socket.socket() as check:
                    check.bind(("127.0.0.1", self.port))
            except OSError:
                raise RuntimeErrorDetail("组件端口已占用；保留现有服务，请换端口或连接已有 Manager。") from None
            self._prepare_config()
            empty_ui = self.runtime_dir / "empty-ui"
            _private_dir(empty_ui)
            environment = {k: v for k, v in os.environ.items()
                           if not k.startswith("ABV_") and k not in {"API_KEY", "WEB_PASSWORD", "AUTH_MODE"}}
            environment.update(ABV_DATA_DIR=str(self.sidecar_dir), ABV_BIND_LOCAL_ONLY="1",
                               ABV_API_KEY=self.gateway_key, ABV_WEB_PASSWORD=self.admin_key,
                               ABV_AUTH_MODE="all_except_health", ABV_DIST_PATH=str(empty_ui))
            if platform.system() == "Linux":
                environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            options = {"env": environment, "cwd": str(self.runtime_dir),
                       "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                       "stderr": subprocess.DEVNULL}
            if os.name != "nt":
                options["umask"] = 0o077
            try:
                self._process = subprocess.Popen([str(self._binary), "--headless"], **options)
            except OSError:
                raise RuntimeErrorDetail("组件无法启动；请检查系统运行权限与平台依赖。") from None
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    if self._process.poll() is not None:
                        raise RuntimeErrorDetail("组件启动后退出；请检查本机系统依赖与私有组件日志。")
                    try:
                        if self._probe():
                            self._stopped = False
                            return self.describe()
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
                    time.sleep(0.1)
                raise RuntimeErrorDetail("组件未在限定时间内就绪，已停止本次启动的进程。")
            except Exception:
                self.stop()
                raise

    def stop(self) -> dict:
        with self._lock:
            process = self._process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                self._process = None
                self._stopped = True
            return self.describe()
