"""Offline configuration fixtures; no desktop control, gateway, or model calls.

The desktop helper requires Python 3.11. Keep discovery usable on the legacy
CLI's Python 3.10 by skipping this module before importing tomllib/the helper.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

if sys.version_info < (3, 11):
    raise unittest.SkipTest("Codex desktop configuration requires Python 3.11+")

import tomllib

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_desktop.py"
spec = importlib.util.spec_from_file_location("codex_desktop", SCRIPT)
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class CodexDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "codex" / "config.toml"
        self.config.parent.mkdir()
        # Windows forbids double quotes in filenames; still exercise spaces,
        # quoting and Unicode there without making fixture creation invalid.
        private_folder = "private space 'quoted' 测试" if os.name == "nt" else 'private space "quoted"'
        self.state = self.root / private_folder / "state.json"
        self.state.parent.mkdir()
        self.secret = "offline-fixture-gateway-secret-only"
        self.state.write_text(json.dumps({"gateway_key": self.secret, "upstream_token": "never-read-this"}))
        self.state.chmod(0o600)
        self.model = "gemini-4-argon-high"
        self.url = "http://127.0.0.1:8787/v1"

    def tearDown(self):
        self.temp.cleanup()

    def preview(self, **kwargs):
        return desktop.preview(self.config, self.state, self.url, self.model, **kwargs)

    def apply(self, **kwargs):
        return desktop.apply(self.config, self.state, self.url, self.model, **kwargs)

    def load(self):
        return tomllib.loads(self.config.read_text())

    def test_preview_is_read_only_and_does_not_read_private_state(self):
        original = b'model = "existing" # untouched\n[features]\nunknown = true\n'
        self.config.write_bytes(original)
        actual_read = desktop._read

        def checked_read(path, **kwargs):
            self.assertNotEqual(Path(path), self.state)
            return actual_read(path, **kwargs)

        with mock.patch.object(desktop, "_read", side_effect=checked_read):
            plan = self.preview()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.config.parent / desktop.BACKUP_DIR).exists())
        self.assertTrue(plan["provider_added"])
        self.assertFalse(plan["default_changed"])
        self.assertFalse(plan["desktop_verified"])
        self.assertNotIn(self.secret, json.dumps(plan))

    def test_preserves_complex_unknown_toml_and_default_byte_for_byte(self):
        original = '''# Existing configuration and comment remain exactly intact.
"model" = 'gpt-existing' # a model comment
model_provider = "openai"
unusual = { nested = { settings = [1, 2, 3] } }
description = """A multiline string containing
[not.a.real.table]
model = 'not a root setting'
with an escaped quote: \\" and a final quote: """"
literal = ''' + "'''a literal\n[also.not.a.table]\nvalue='stay'\n'''" + '''
list = [
  "[brackets]", # # comment, containing a closing bracket ]
  { nested = "a # and = in a string" },
]
[model_providers.old]
name = "Keep this provider"
base_url = "https://example.invalid/v1"
[model_providers.old.http_headers]
"X-Safe" = "keep"
[[profiles.demo.items]]
name = "first"
[[profiles.demo.items]]
name = "second"
'''
        tomllib.loads(original)
        self.config.write_text(original)
        receipt = self.apply()
        self.assertTrue(self.config.read_text().startswith(original))
        parsed = self.load()
        self.assertEqual(parsed["model"], "gpt-existing")
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["profiles"]["demo"]["items"][1]["name"], "second")
        self.assertFalse(receipt["activate"])

    def test_provider_uses_captured_python_helper_and_native_catalog(self):
        self.apply()
        provider = self.load()["model_providers"][desktop.PROVIDER_ID]
        self.assertEqual(provider["base_url"], self.url)
        self.assertEqual(provider["model_catalog_url"], "http://127.0.0.1:8787/codex/models")
        self.assertEqual(provider["wire_api"], "responses")
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["auth"]["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(provider["auth"]["args"], [str(SCRIPT), "credential", "--state", str(self.state)])
        self.assertNotIn("env_key", provider)
        self.assertNotIn("requires_openai_auth", provider)
        self.assertNotIn("experimental_bearer_token", provider)
        self.assertNotIn("model_catalog_json", self.load())
        self.assertNotIn("model", self.load())

    def test_activation_changes_root_values_preserves_quoted_keys_and_comments(self):
        original = b'# Header\r\n"model" \t= \'old-model\'   # retained comment\r\n\'model_provider\' = "old-provider"\r\n[profiles.demo]\r\nmodel = "profile-kept"\r\n'
        self.config.write_bytes(original)
        receipt = self.apply(activate=True)
        changed = self.config.read_bytes()
        self.assertIn(b'"model" \t= "gemini-4-argon-high"   # retained comment\r\n', changed)
        self.assertIn(b'\'model_provider\' = "gemini_agent_kit"\r\n', changed)
        self.assertIn(b'[profiles.demo]\r\nmodel = "profile-kept"\r\n', changed)
        self.assertEqual(self.load()["model"], self.model)
        self.assertEqual(self.load()["profiles"]["demo"]["model"], "profile-kept")
        desktop.restore(self.config, receipt["receipt_path"])
        self.assertEqual(self.config.read_bytes(), original)

    def test_activation_handles_multiline_values_and_inserted_missing_root(self):
        original = 'model = """\nold model with # and =\n[inside.string]\n"""  # keep\n[features]\nx = true\n'
        self.config.write_text(original)
        receipt = self.apply(activate=True)
        updated = self.config.read_text()
        self.assertTrue(updated.startswith('model_provider = "gemini_agent_kit"\n'))
        self.assertIn('model = "gemini-4-argon-high"  # keep', updated)
        self.assertTrue(self.load()["features"]["x"])
        desktop.restore(self.config, receipt["receipt_path"])
        self.assertEqual(self.config.read_text(), original)

    def test_activation_inserts_before_tables_with_no_root_keys(self):
        self.config.write_text('[features]\nfoo = true\n')
        self.apply(activate=True)
        parsed = self.load()
        self.assertEqual(parsed["model"], self.model)
        self.assertEqual(parsed["model_provider"], desktop.PROVIDER_ID)
        self.assertEqual(parsed["features"], {"foo": True})

    def test_idempotent_apply_has_no_duplicate_tables_or_new_receipt(self):
        first = self.apply()
        original = self.config.read_bytes()
        files = sorted((self.config.parent / desktop.BACKUP_DIR).iterdir())
        second = self.apply()
        self.assertFalse(second["changed"])
        self.assertIsNone(second["receipt_path"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted((self.config.parent / desktop.BACKUP_DIR).iterdir()), files)
        self.assertFalse(self.preview()["changes"])
        self.assertEqual(self.preview()["diff"], "")
        self.assertEqual(first["status"], "applied")

    def test_activate_after_provider_only_install_then_restore_each_step(self):
        original = 'model = "original"\n'
        self.config.write_text(original)
        provider_receipt = self.apply()
        provider_only = self.config.read_bytes()
        activated = self.apply(activate=True)
        self.assertEqual(self.load()["model"], self.model)
        self.assertFalse(self.apply(activate=True)["changed"])
        desktop.restore(self.config, activated["receipt_path"])
        self.assertEqual(self.config.read_bytes(), provider_only)
        desktop.restore(self.config, provider_receipt["receipt_path"])
        self.assertEqual(self.config.read_text(), original)

    def test_conflicting_provider_never_silently_overwritten(self):
        original = '[model_providers.gemini_agent_kit]\nbase_url = "https://other.invalid/v1"\n'
        self.config.write_text(original)
        with self.assertRaises(desktop.ConfigError):
            self.apply()
        self.assertEqual(self.config.read_text(), original)

    def test_existing_unknown_provider_setting_is_preserved_but_auth_conflict_rejected(self):
        self.apply()
        text = self.config.read_text().replace('name = "Gemini Agent Kit"', 'name = "Gemini Agent Kit"\ncustom_unknown = "retained"')
        self.config.write_text(text)
        self.assertFalse(self.apply()["changed"])
        self.assertEqual(self.config.read_text(), text)
        text = text.replace('custom_unknown = "retained"', 'requires_openai_auth = false')
        self.config.write_text(text)
        with self.assertRaises(desktop.ConfigError):
            self.preview()
        self.assertEqual(self.config.read_text(), text)

    def test_existing_global_catalog_is_preserved_and_warning_returned(self):
        original = 'model_catalog_json = "/existing/private-catalog.json"\n'
        self.config.write_text(original)
        plan = self.preview()
        self.assertEqual(len(plan["warnings"]), 1)
        self.apply()
        self.assertTrue(self.config.read_text().startswith(original))
        self.assertEqual(self.load()["model_catalog_json"], "/existing/private-catalog.json")

    def test_invalid_toml_or_nonextensible_inline_table_does_not_overwrite(self):
        for text in ('model = "broken', 'model_providers = { old = { name = "keep" } }\n'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(desktop.ConfigError):
                    self.apply()
                self.assertEqual(self.config.read_text(), text)

    def test_restore_exact_backup_and_permissions_are_private(self):
        original = b'# original\nmodel = "original"\n'
        self.config.write_bytes(original)
        receipt = self.apply()
        source = Path(receipt["receipt_path"])
        backup = source.parent / receipt["backup_file"]
        self.assertEqual(backup.read_bytes(), original)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        result = desktop.restore(self.config, source)
        self.assertTrue(result["restored"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(desktop.restore(self.config, source)["changed"])

    def test_new_file_restore_removes_only_unchanged_created_config(self):
        plan = self.preview()
        self.assertIsNone(plan["before_sha256"])
        receipt = self.apply(expected_before_sha256=None)
        self.assertIsNone(receipt["backup_file"])
        desktop.restore(self.config, receipt["receipt_path"])
        self.assertFalse(self.config.exists())

    def test_preview_hash_rejects_edit_before_apply(self):
        self.config.write_text('model = "before"\n')
        reviewed = self.preview()
        changed = 'model = "user-changed-it"\n'
        self.config.write_text(changed)
        with self.assertRaisesRegex(desktop.ConfigError, "after preview"):
            self.apply(expected_before_sha256=reviewed["before_sha256"])
        self.assertEqual(self.config.read_text(), changed)
        self.assertEqual(list((self.config.parent / desktop.BACKUP_DIR).glob("*.receipt.json")), [])

    def test_expected_missing_rejects_file_created_after_preview(self):
        self.config.write_text('model = "new user configuration"\n')
        with self.assertRaises(desktop.ConfigError):
            self.apply(expected_before_sha256=None)
        self.assertEqual(self.load()["model"], "new user configuration")

    def test_concurrent_edit_before_atomic_replace_is_preserved(self):
        self.config.write_text('model = "before"\n')
        actual_replace = desktop._replace_if_unchanged
        edited = 'model = "changed while applying"\n'

        def concurrent(path, data, expected, *args):
            if path == self.config:
                self.config.write_text(edited)
            return actual_replace(path, data, expected, *args)

        with mock.patch.object(desktop, "_replace_if_unchanged", side_effect=concurrent):
            with self.assertRaisesRegex(desktop.ConfigError, "concurrently"):
                self.apply()
        self.assertEqual(self.config.read_text(), edited)
        self.assertEqual(list(self.config.parent.glob(".config.toml.kit-*")), [])

    def test_restore_refuses_post_install_user_edits(self):
        self.config.write_text('model = "before"\n')
        receipt = self.apply()
        edited = self.config.read_text() + '\n# User changed config after install\n'
        self.config.write_text(edited)
        with self.assertRaisesRegex(desktop.ConfigError, "edited after installation"):
            desktop.restore(self.config, receipt["receipt_path"])
        self.assertEqual(self.config.read_text(), edited)

    def test_restore_detects_corrupt_backup(self):
        self.config.write_text('model = "before"\n')
        receipt = self.apply()
        after = self.config.read_bytes()
        backup = Path(receipt["receipt_path"]).parent / receipt["backup_file"]
        backup.write_text('model = "tampered backup"\n')
        with self.assertRaisesRegex(desktop.ConfigError, "checksum"):
            desktop.restore(self.config, receipt["receipt_path"])
        self.assertEqual(self.config.read_bytes(), after)

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks are unavailable")
    def test_symlink_config_and_state_and_parent_are_refused(self):
        target = self.root / "real.toml"
        target.write_text('model = "do not change"\n')
        self.config.symlink_to(target)
        with self.assertRaises(desktop.ConfigError):
            self.preview()
        self.config.unlink()
        linked_state = self.root / "linked-state.json"
        linked_state.symlink_to(self.state)
        with self.assertRaises(desktop.ConfigError):
            desktop.preview(self.config, linked_state, self.url, self.model)
        linked_dir = self.root / "linked-dir"
        linked_dir.symlink_to(self.config.parent, target_is_directory=True)
        with self.assertRaises(desktop.ConfigError):
            desktop.preview(linked_dir / "config.toml", self.state, self.url, self.model)
        self.assertEqual(target.read_text(), 'model = "do not change"\n')

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks are unavailable")
    def test_symlink_backup_directory_and_receipt_are_refused(self):
        actual = self.root / "other"
        actual.mkdir()
        location = self.config.parent / desktop.BACKUP_DIR
        location.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(desktop.ConfigError):
            self.apply()
        location.unlink()
        receipt = self.apply()
        source = Path(receipt["receipt_path"])
        copy = actual / "receipt.json"
        copy.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(copy)
        with self.assertRaises(desktop.ConfigError):
            desktop.restore(self.config, source)

    def test_lock_prevents_competing_install(self):
        with desktop._lock(self.config):
            with self.assertRaisesRegex(desktop.ConfigError, "holds the lock"):
                self.apply()
        self.assertFalse(self.config.exists())

    def test_no_keys_or_existing_sensitive_comments_in_preview_or_receipt(self):
        old_secret = "existing-api-secret-that-must-remain-local"
        self.config.write_text(f'model = "original" # {old_secret}\n[model_providers.private]\nexperimental_bearer_token = "{old_secret}"\n')
        plan = self.preview(activate=True)
        receipt = self.apply(activate=True)
        public = json.dumps([plan, receipt]) + Path(receipt["receipt_path"]).read_text()
        self.assertNotIn(self.secret, public)
        self.assertNotIn(old_secret, public)
        self.assertNotIn("never-read-this", public)
        self.assertIn(old_secret, self.config.read_text())

    def test_credential_helper_reads_only_gateway_key_and_never_logs_errors(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = desktop.main(["credential", "--state", str(self.state)])
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), self.secret)
        self.assertEqual(errors.getvalue(), "")
        self.state.write_text('{"gateway_key": "' + self.secret + '\ninvalid-json')
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = desktop.main(["credential", "--state", str(self.state)])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(self.secret, errors.getvalue())

    @unittest.skipUnless(os.name == "posix", "Unix file modes")
    def test_credential_refuses_public_state_and_header_injection(self):
        self.state.chmod(0o644)
        with self.assertRaises(desktop.ConfigError):
            desktop._credential(self.state)
        self.state.chmod(0o600)
        for value in ("key\r\nInjected: yes", " key", "", None):
            self.state.write_text(json.dumps({"gateway_key": value}))
            with self.assertRaises(desktop.ConfigError):
                desktop._credential(self.state)

    def test_endpoint_rejects_credentials_remote_urls_and_query_secrets(self):
        for url in ("http://127.0.0.1:8787/v1?api_key=secret", "http://name:pass@127.0.0.1:8787/v1",
                    "https://outside.invalid/v1", "http://127.0.0.1/v1", "http://127.0.0.1:8787/other"):
            with self.subTest(url=url):
                with self.assertRaises(desktop.ConfigError):
                    desktop.preview(self.config, self.state, url, self.model)


if __name__ == "__main__":
    unittest.main()
