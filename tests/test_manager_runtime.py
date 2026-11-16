"""Lifecycle/security fixtures: no backend, account, download, or model is contacted."""
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import socket
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manager_runtime.py"
spec = importlib.util.spec_from_file_location("manager_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


@unittest.skipIf(sys.version_info < (3, 11), "Local GUI requires Python 3.11+")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def manager(self):
        return runtime.ManagerRuntime(self.root / "private", port=18879)

    def fake_binary(self, manager):
        manager._directories()
        binary = manager.runtime_dir / "fixture-binary-never-executed"
        binary.write_bytes(b"fixture")
        manager._binary = binary
        return binary

    def archive(self, members):
        path = self.root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = 4
                    archive.addfile(info, io.BytesIO(b"test"))
                else:
                    info.type = kind
                    info.linkname = "/tmp/fixture-target"
                    archive.addfile(info)
        return path

    def test_no_keys_in_description_and_distinct_private_keys(self):
        manager = self.manager()
        self.assertFalse(manager.data_dir.exists())
        manager._prepare_config()
        self.assertNotEqual(manager.gateway_key, manager.admin_key)
        self.assertGreater(len(manager.gateway_key), 30)
        described = json.dumps(manager.describe())
        self.assertNotIn(manager.gateway_key, described)
        self.assertNotIn(manager.admin_key, described)
        self.assertEqual(manager.describe()["base_url"], "http://127.0.0.1:18879")
        reopened = self.manager()
        self.assertEqual(reopened.gateway_key, manager.gateway_key)
        self.assertEqual(reopened.admin_key, manager.admin_key)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(manager.config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manager.sidecar_dir.stat().st_mode), 0o700)

    def test_initial_config_disables_lan_and_background_generation(self):
        manager = self.manager()
        manager._prepare_config()
        config = json.loads(manager.config_path.read_text())
        self.assertFalse(config["scheduled_warmup"]["enabled"])
        self.assertFalse(config["auto_sync"])
        self.assertFalse(config["auto_refresh"])
        self.assertFalse(config["proxy"]["allow_lan_access"])
        self.assertFalse(config["proxy"]["enable_logging"])
        self.assertEqual(config["proxy"]["auth_mode"], "all_except_health")
        config["custom_setting"] = "preserved"
        runtime._write_private(manager.config_path, config)
        manager._prepare_config()
        self.assertEqual(json.loads(manager.config_path.read_text())["custom_setting"], "preserved")

    def test_symlink_config_refused_without_touching_target(self):
        if os.name == "nt":
            self.skipTest("Creating symlinks may need Windows privileges")
        manager = self.manager()
        manager._directories()
        target = self.root / "outside.json"
        target.write_text('{"sentinel":true}')
        manager.config_path.symlink_to(target)
        with self.assertRaises(runtime.RuntimeErrorDetail):
            manager._prepare_config()
        self.assertEqual(target.read_text(), '{"sentinel":true}')

    def test_reject_unsafe_archive_names_and_links_before_any_extraction(self):
        for name, kind in [("../outside", "file"), ("/absolute", "file"),
                           (r"..\outside", "file"), ("C:/outside", "file"),
                           ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE),
                           ("pipe", tarfile.FIFOTYPE)]:
            with self.subTest(name=name, kind=kind):
                archive = self.archive([("safe", "file"), (name, kind)])
                target = self.root / "unpacked"
                with self.assertRaises(runtime.RuntimeErrorDetail):
                    runtime._extract_tar(archive, target)
                self.assertFalse((target / "safe").exists())

    def test_safe_archive_extraction(self):
        archive = self.archive([("app/data.txt", "file")])
        runtime._extract_tar(archive, self.root / "unpacked")
        self.assertEqual((self.root / "unpacked/app/data.txt").read_bytes(), b"test")

    def test_download_hosts_and_redirects(self):
        for url in ["http://github.com/file", "https://github.com.evil.test/file",
                    "https://user:secret@github.com/file", "https://github.com:444/file",
                    "https://127.0.0.1/file"]:
            with self.subTest(url=url), self.assertRaises(runtime.RuntimeErrorDetail):
                runtime._release_url(url)
        self.assertEqual(runtime._release_url(runtime.WINDOWS_INSTALLER), runtime.WINDOWS_INSTALLER)
        handler = runtime._ReleaseRedirect()
        with self.assertRaises(runtime.RuntimeErrorDetail):
            handler.redirect_request(None, None, 302, "", {}, "https://evil.test/file")

    def test_download_checksum_failure_removes_new_file_only(self):
        response = MagicMock()
        response.geturl.return_value = runtime.DOWNLOAD_ROOT + "fixture"
        response.read.side_effect = [b"fixture", b""]
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        target = self.root / "download"
        with patch.object(runtime.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(runtime.RuntimeErrorDetail):
                runtime._download(runtime.DOWNLOAD_ROOT + "fixture", target, "0" * 64)
            self.assertFalse(target.exists())
            target.write_bytes(b"existing")
            with self.assertRaises(runtime.RuntimeErrorDetail):
                runtime._download(runtime.DOWNLOAD_ROOT + "fixture", target, "0" * 64)
            self.assertEqual(target.read_bytes(), b"existing")
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_setup_verified_download_does_not_execute(self):
        manager = self.manager()
        body = b"mock-appimage-never-run"
        digest = hashlib.sha256(body).hexdigest()
        def download(url, target, expected):
            self.assertEqual(expected, digest)
            target.write_bytes(body)
        with patch.object(runtime.platform, "system", return_value="Linux"), \
             patch.object(runtime.platform, "machine", return_value="x86_64"), \
             patch.dict(runtime.ASSETS, {("Linux", "x86_64"): ("fixture.AppImage", digest)}), \
             patch.object(runtime, "_download", side_effect=download), \
             patch.object(runtime.subprocess, "Popen") as popen:
            self.assertEqual(manager.setup()["status"], "installed")
            self.assertEqual(manager._binary.read_bytes(), body)
            self.assertEqual(manager.setup()["status"], "installed")
            popen.assert_not_called()
            self.assertFalse(manager.config_path.exists())

    def test_windows_missing_returns_instructions_without_running_installer(self):
        manager = self.manager()
        with patch.object(runtime.platform, "system", return_value="Windows"), \
             patch.object(runtime, "_windows_binary", return_value=None), \
             patch.object(runtime.subprocess, "Popen") as popen, \
             patch.object(runtime, "_download") as download:
            info = manager.setup()
            self.assertEqual(info["status"], "missing")
            self.assertEqual(info["download_url"], runtime.WINDOWS_INSTALLER)
            self.assertIn("安装", info["instructions"])
            popen.assert_not_called()
            download.assert_not_called()

    def test_mac_installed_version_must_match(self):
        app = self.root / "Antigravity Tools.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/antigravity-tools").write_bytes(b"fixture")
        info = app / "Contents/Info.plist"
        info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "5.0.3",
                                        "CFBundleExecutable": "antigravity-tools"}))
        self.assertIsNotNone(runtime._mac_binary(app))
        info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "5.0.4",
                                        "CFBundleExecutable": "antigravity-tools"}))
        self.assertIsNone(runtime._mac_binary(app))

    def test_occupied_port_preserves_other_service_and_config(self):
        with socket.socket() as existing:
            existing.bind(("127.0.0.1", 0))
            manager = runtime.ManagerRuntime(self.root / "private", existing.getsockname()[1])
            self.fake_binary(manager)
            with patch.object(runtime.platform, "system", return_value="Linux"), \
                 patch.object(runtime.subprocess, "Popen") as popen:
                with self.assertRaises(runtime.RuntimeErrorDetail):
                    manager.start()
                manager.stop()
                popen.assert_not_called()
                self.assertFalse(manager.config_path.exists())
                self.assertGreater(existing.fileno(), -1)

    def test_start_isolated_env_and_stop_only_owned_process(self):
        manager = self.manager()
        binary = self.fake_binary(manager)
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(runtime.platform, "system", return_value="Linux"), \
             patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
             patch.object(manager, "_probe", return_value=True), \
             patch.dict(os.environ, {"ABV_DATA_DIR": "/other/private", "ABV_WEB_PASSWORD": "foreign",
                                     "ABV_DATA_DIR_POINTER_FILE": "/other/pointer", "API_KEY": "foreign"}):
            self.assertEqual(manager.start()["status"], "running")
            call = popen.call_args
            self.assertEqual(call.args[0], [str(binary), "--headless"])
            env = call.kwargs["env"]
            self.assertEqual(env["ABV_DATA_DIR"], str(manager.sidecar_dir))
            self.assertEqual(env["ABV_BIND_LOCAL_ONLY"], "1")
            self.assertEqual(env["ABV_WEB_PASSWORD"], manager.admin_key)
            self.assertEqual(env["ABV_API_KEY"], manager.gateway_key)
            self.assertNotIn("ABV_DATA_DIR_POINTER_FILE", env)
            self.assertNotIn("API_KEY", env)
            manager.start()
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(manager.stop()["status"], "stopped")
            manager.stop()
            self.assertEqual(process.terminate.call_count, 1)

    def test_failed_start_stops_its_process(self):
        manager = self.manager()
        self.fake_binary(manager)
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(runtime.platform, "system", return_value="Linux"), \
             patch.object(runtime.subprocess, "Popen", return_value=process), \
             patch.object(manager, "_probe", side_effect=runtime.RuntimeErrorDetail("version mismatch")):
            with self.assertRaises(runtime.RuntimeErrorDetail):
                manager.start()
            process.terminate.assert_called_once()
            self.assertEqual(manager.describe()["status"], "stopped")

    def test_empty_install_readiness_uses_authenticated_management_routes(self):
        calls = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                # The real upstream keeps its proxy disabled with zero accounts.
                # /v1/version also returns 503 in that state, so probe admin health.
                if self.path not in ("/api/proxy/status", "/api/health"):
                    self.send_error(503)
                    return
                value = ({"port": self.server.server_port, "running": False, "active_accounts": 0}
                         if self.path == "/api/proxy/status" else {
                             "status": self.server.health_status, "version": self.server.component_version})
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server.health_status = "ok"
        server.component_version = "5.0.3"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            manager = runtime.ManagerRuntime(self.root / "private", server.server_port)
            manager.admin_key = "fixture-admin"
            manager.gateway_key = "fixture-gateway"
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1"}):
                self.assertTrue(manager._probe())
            self.assertEqual(calls, [("/api/proxy/status", "Bearer fixture-admin"),
                                     ("/api/health", "Bearer fixture-admin")])
            server.health_status = "starting"
            self.assertFalse(manager._probe())
            server.health_status = "ok"
            server.component_version = "5.0.4"
            with self.assertRaises(runtime.RuntimeErrorDetail):
                manager._probe()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unowned_runtime_stop_never_terminates_anything(self):
        manager = self.manager()
        with patch.object(runtime.subprocess, "Popen") as popen, patch.object(os, "kill") as kill:
            self.assertEqual(manager.stop()["status"], "missing")
            popen.assert_not_called()
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
