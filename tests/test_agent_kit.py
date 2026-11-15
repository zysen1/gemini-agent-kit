"""Local protocol fixtures only: these tests do not contact a model provider."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_kit.py"
spec = importlib.util.spec_from_file_location("agent_kit", SCRIPT)
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


class Fixture(BaseHTTPRequestHandler):
    calls = []
    mode = "ok"
    models = ["gateway/google/gemini-4-argon", "gateway/google/gemini-4-hydrogen"]
    secret = "test-secret-not-a-real-key"

    def log_message(self, *args):
        pass

    def respond(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/api/hello":
            return self.respond(200, {"name": "fixture-not-magpie", "version": "0-test"})
        if self.mode == "timeout":
            time.sleep(0.15)
        if self.mode == "auth":
            return self.respond(401, {"error": "denied Bearer " + self.secret, "api_key": self.secret})
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", self.server.other_url)
            self.end_headers()
            return
        return self.respond(200, {"data": [{"id": x} for x in self.models]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(body)
        if self.mode == "auth":
            return self.respond(401, {"error": "Bearer " + self.secret})
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            # Verify exact metadata and call ID survived the client round trip.
            assert messages[-2]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "fixture-signature"}}
            assert messages[-1]["tool_call_id"] == "call-fixture"
            output = json.loads(messages[-1]["content"])["proof"]
            message = {"role": "assistant", "content": output}
        elif body.get("tools"):
            if self.mode == "no_tools":
                message = {"role": "assistant", "content": "I will call the tool (but did not)."}
            else:
                nonce = messages[0]["content"].split("nonce ")[1].split(".")[0]
                if self.mode == "wrong_nonce":
                    nonce = "wrong"
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-fixture", "type": "function", "function": {"name": "kit_probe", "arguments": json.dumps({"nonce": nonce})},
                    "extra_content": {"google": {"thought_signature": "fixture-signature"}}}]}
        else:
            content = messages[-1]["content"]
            output = content.split("nothing else: ")[-1] if "nothing else: " in content else "fixture answer"
            message = {"role": "assistant", "content": output}
        self.respond(200, {"model": "fixture-reported-model", "choices": [{"message": message}], "usage": {"total_tokens": 1}})


class TestKit(unittest.TestCase):
    def setUp(self):
        # Each server owns its state, including delayed request handlers. This
        # prevents a timed-out handler from seeing a later test's configuration.
        self.fixture = type("IsolatedFixture", (Fixture,), {"mode": "ok", "calls": [], "models": list(Fixture.models)})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.fixture)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.gateway = kit.Gateway(self.url, environ={kit.KEY_ENV: Fixture.secret})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_dynamic_catalog_exact_and_unique_argon(self):
        models = self.gateway.models()
        self.assertEqual(kit.select_model(models, None), Fixture.models[0])
        self.assertEqual(kit.select_model(models, Fixture.models[1]), Fixture.models[1])
        with self.assertRaisesRegex(kit.KitError, "absent"):
            kit.select_model(models, "gemini-4-carbon")
        with self.assertRaisesRegex(kit.KitError, "exact ID"):
            kit.select_model(models + [{"id": "another/gemini-4-argon"}], None)

    def test_doctor_empty_catalog_is_reachable_but_not_ready(self):
        self.fixture.models = []
        report = kit.doctor(self.gateway)
        self.assertTrue(report["ok"])
        self.assertTrue(report["gateway_reachable"])
        self.assertFalse(report["catalog_has_models"])
        self.assertEqual(report["model_count"], 0)
        self.assertFalse(report["inference_requested"])
        self.assertIn("empty", report["warning"])
        self.assertFalse(self.fixture.calls)

    def test_http_policy_and_no_credentials_in_url(self):
        for url in ("http://example.com/v1", "http://127.0.0.1.evil.test/v1", "http://user:password@localhost/v1", "https://example.com/v1?key=secret"):
            with self.subTest(url=url), self.assertRaises(kit.KitError):
                kit.Gateway(url, environ={kit.KEY_ENV: "secret"})
        remote = kit.Gateway("http://example.com/v1", allow_insecure=True, environ={kit.KEY_ENV: "secret"})
        self.assertFalse(remote.local)
        with self.assertRaisesRegex(kit.KitError, "Set"):
            kit.Gateway("https://example.com/v1", environ={})

    def test_redirect_is_not_followed(self):
        self.fixture.mode = "redirect"
        self.server.other_url = self.url + "/never-follow"
        with self.assertRaisesRegex(kit.KitError, "302"):
            self.gateway.models()

    def test_timeout_is_bounded_and_named(self):
        self.fixture.mode = "timeout"
        fast = kit.Gateway(self.url, timeout=0.03, environ={})
        with self.assertRaises(kit.KitError) as raised:
            fast.models()
        self.assertEqual(raised.exception.code, "TIMEOUT")

    def test_nan_timeout_is_rejected_before_transport(self):
        with self.assertRaises(kit.KitError) as raised:
            kit.Gateway(self.url, timeout=float("nan"), environ={})
        self.assertEqual(raised.exception.code, "INVALID_TIMEOUT")

    def test_auth_error_redacts_echoed_token(self):
        self.fixture.mode = "auth"
        with self.assertRaises(kit.KitError) as raised:
            self.gateway.models()
        self.assertNotIn(Fixture.secret, str(raised.exception))
        self.assertIn("401", str(raised.exception))

    def test_live_probe_protocol_fixture_round_trip(self):
        report = kit.verify(self.gateway, Fixture.models[0])
        self.assertTrue(report["passed"])
        self.assertEqual(report["request_count"], 3)
        self.assertFalse(report["client_end_to_end_verified"])
        self.assertEqual(len(self.fixture.calls), 3)
        tool_result = json.loads(self.fixture.calls[2]["messages"][-1]["content"])
        self.assertEqual(len(tool_result["proof"]), 64)

    def test_tool_promise_is_not_execution(self):
        self.fixture.mode = "no_tools"
        report = kit.verify(self.gateway, Fixture.models[0])
        self.assertFalse(report["passed"])
        self.assertEqual(report["tests"][1]["error"]["code"], "TOOL_CALL_MISSING")
        self.assertEqual(len(self.fixture.calls), 2)

    def test_wrong_tool_arguments_are_not_executed(self):
        self.fixture.mode = "wrong_nonce"
        report = kit.verify(self.gateway, Fixture.models[0])
        self.assertFalse(report["passed"])
        self.assertEqual(report["tests"][1]["error"]["code"], "TOOL_ARGUMENTS_INVALID")
        self.assertEqual(len(self.fixture.calls), 2)

    def test_plans_do_not_contain_credentials_or_claim_client_success(self):
        for client in ("codex", "workbuddy", "muse", "grokbot"):
            with self.subTest(client=client):
                plan = kit.make_plan(self.gateway, Fixture.models[0], client)
                self.assertNotIn(Fixture.secret, json.dumps(plan))
                self.assertEqual(plan["status"], "configuration_plan_only")
                if client in ("muse", "grokbot"):
                    self.assertEqual(plan["mode"], "external_tool_delegation")
                    self.assertIn("cannot use your Mac", plan["host_boundary"])

    def test_cloud_plan_preserves_timeout_and_explicit_http_choice(self):
        remote=kit.Gateway("http://192.0.2.20/v1", timeout=90, allow_insecure=True, environ={kit.KEY_ENV: Fixture.secret})
        plan=kit.make_plan(remote,Fixture.models[0],"grokbot","/home/box/kit/scripts/agent_kit.py")
        args=plan["mcp_server"]["args"]
        self.assertEqual(args[args.index("--timeout")+1],"90")
        self.assertIn("--allow-insecure-http",args)
        self.assertEqual(args[0],"/home/box/kit/scripts/agent_kit.py")
        self.assertNotIn(Fixture.secret,json.dumps(plan))

    def test_report_redaction_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/"report.json"
            kit.save_json(str(target), {"message": Fixture.secret, "nested": {"api_key": "another"}}, Fixture.secret)
            saved = target.read_text()
            self.assertNotIn(Fixture.secret, saved)
            self.assertNotIn("another", saved)
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(kit.KitError):
                kit.save_json(str(target), {})

    def test_mcp_protocol_only_stdout_and_metadata(self):
        incoming = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ask_gemini", "arguments": {"prompt": "hello"}}},
        ]
        inp=io.StringIO("\n".join(json.dumps(x) for x in incoming)+"\n")
        out=io.StringIO()
        kit.mcp_serve(self.gateway, None, inp, out)
        rows=[json.loads(s) for s in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in rows], [1,2,3])
        self.assertEqual(rows[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(rows[1]["result"]["tools"][0]["name"], "ask_gemini")
        data=json.loads(rows[2]["result"]["content"][0]["text"])
        self.assertEqual(data["provider_reported_model"], "fixture-reported-model")

    def test_mcp_invalid_json_and_invalid_arguments(self):
        rows=[{"jsonrpc":"2.0","id":1,"method":"initialize"},
              {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ask_gemini","arguments":{"prompt":"hi","max_tokens":True}}}]
        out=io.StringIO()
        kit.mcp_serve(self.gateway,None,io.StringIO("oops\n"+"\n".join(json.dumps(x) for x in rows)),out)
        actual=[json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(actual[0]["error"]["code"],-32700)
        self.assertEqual(actual[-1]["error"]["code"],-32602)
        self.assertFalse(self.fixture.calls)

    def test_codex_argv_never_contains_token_or_edits_config(self):
        cmd=kit.codex_command(self.gateway,Fixture.models[0],["--","exec","read README"])
        self.assertNotIn(Fixture.secret," ".join(cmd))
        self.assertIn('model_providers.magpie_kit.wire_api="responses"',cmd)
        self.assertEqual(cmd[-2:],["exec","read README"])

    def test_cli_options_before_and_after_subcommand(self):
        env=os.environ.copy()
        env[kit.KEY_ENV]=Fixture.secret
        for args in (["--base-url",self.url,"models"],["models","--base-url",self.url]):
            run=subprocess.run([sys.executable,str(SCRIPT),*args],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout)["models"][0]["id"],Fixture.models[0])

    def test_cli_verify_saves_fixture_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/"fixture.json"
            env=os.environ.copy(); env[kit.KEY_ENV]=Fixture.secret
            run=subprocess.run([sys.executable,str(SCRIPT),"verify","--base-url",self.url,"--output",str(report)],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertTrue(json.loads(report.read_text())["passed"])
            self.assertNotIn(Fixture.secret,report.read_text()+run.stdout+run.stderr)

    def test_existing_report_is_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/"keep.json"
            report.write_text("do not change")
            env=os.environ.copy(); env[kit.KEY_ENV]=Fixture.secret
            run=subprocess.run([sys.executable,str(SCRIPT),"verify","--base-url",self.url,"--output",str(report)],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,1)
            self.assertEqual(json.loads(run.stderr)["error"],"OUTPUT_EXISTS")
            self.assertEqual(report.read_text(),"do not change")
            self.assertFalse(self.fixture.calls)

    def test_mcp_as_real_stdio_subprocess(self):
        env=os.environ.copy(); env[kit.KEY_ENV]=Fixture.secret
        reqs=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":[]}},
              {"jsonrpc":"2.0","id":2,"method":"tools/list"},
              {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ask_gemini","arguments":{"prompt":"中文任务与emoji 🧪"}}}]
        run=subprocess.run([sys.executable,str(SCRIPT),"mcp","--base-url",self.url],input="\n".join(json.dumps(x) for x in reqs)+"\n",env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(run.stderr,"")
        replies=[json.loads(x) for x in run.stdout.splitlines()]
        self.assertEqual(len(replies),3)
        self.assertFalse(replies[-1]["result"]["isError"])
        self.assertEqual(self.fixture.calls[-1]["messages"][0]["content"],"中文任务与emoji 🧪")


if __name__ == "__main__":
    unittest.main()
