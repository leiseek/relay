"""Local fake-CLI regression tests. No model requests or real quota usage."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_recovery import detect_limit, json_objects
from run_agent import select_model

RUNNER = SCRIPTS / "run_agent.py"
GIT = shutil.which("git")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def emit(record):
    return f"print({json.dumps(record, ensure_ascii=False)!r}, flush=True)\n"


@unittest.skipUnless(os.name != "nt" or shutil.which("pwsh.exe"), "Windows shim requires pwsh")
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="external-recovery-qa-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "workspace 中文 spaces"
        self.bin = self.root / "bin"
        self.work.mkdir()
        self.bin.mkdir()
        paths = [str(self.bin), str(Path(sys.executable).parent)]
        if os.name == "nt":
            paths += [str(Path(shutil.which("pwsh.exe")).parent), str(Path(os.environ["SystemRoot"]) / "System32")]
        else:
            paths.extend(os.defpath.split(os.pathsep))
        if GIT:
            paths.append(str(Path(GIT).parent))
        self.env = dict(os.environ, PATH=os.pathsep.join(paths), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")

    def fake(self, engine, script):
        payload = self.bin / (engine + ".py")
        payload.write_text("import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n" + script, encoding="utf-8")
        if os.name == "nt":
            command = f"& {ps_literal(sys.executable)} {ps_literal(payload)} @args\nexit $LASTEXITCODE\n"
            (self.bin / (engine + ".ps1")).write_text(command, encoding="utf-8")
        else:
            shim = self.bin / engine
            shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(payload))} "$@"\n', encoding="utf-8")
            shim.chmod(0o755)

    def cli_args(self, engine, prompt, extra=()):
        entry = [sys.executable, "-B", str(RUNNER)] if os.name == "nt" else ["sh", str(RUNNER.with_suffix(".sh"))]
        return [*entry, "--engine", engine, "--cwd", str(self.work), "--prompt", prompt,
                "--task-mode", "edit", "--timeout", "20", *extra]

    def run_cli(self, engine, prompt="Finish the delegated task.", output=True, extra=()):
        cmd = self.cli_args(engine, prompt, extra)
        if output:
            cmd += ["--output-dir", str(self.root / "run")]
        run = subprocess.run(cmd, env=self.env, capture_output=True, encoding="utf-8", timeout=35)
        self.assertTrue(run.stdout.strip(), run.stderr)
        result = json.loads(run.stdout)
        return run, result

    def assert_takeover(self, run, result, kind):
        self.assertEqual(run.returncode, 75, (run.stdout, run.stderr))
        self.assertEqual(result["status"], "needs_takeover")
        self.assertEqual(result["failureKind"], kind)
        self.assertTrue(result["takeoverRequired"])
        self.assertTrue(result["workerStopped"])
        self.assertEqual(result["nextAction"], "continue_in_parent")
        self.assertTrue(Path(result["handoffFile"]).is_file())

    @unittest.skipUnless(GIT, "Git evidence requires git")
    def test_agy_midrun_quota_preserves_dirty_work_and_partial_steps(self):
        target = self.work / "arithmetic.py"
        initial = "# original header\ndef add(a, b):\n    raise NotImplementedError\n\ndef subtract(a, b):\n    raise NotImplementedError\n"
        target.write_text(initial, encoding="utf-8")
        for command in (["init", "--quiet"], ["add", "arithmetic.py"],
                        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture"]):
            subprocess.run([GIT, "-C", str(self.work), *command], check=True, capture_output=True)
        target.write_text(initial.replace("# original header", "# user note: preserve this header"), encoding="utf-8")
        partial = target.read_text(encoding="utf-8").replace("    raise NotImplementedError", "    return a + b", 1)
        script = f"Path({str(target)!r}).write_text({partial!r}, encoding='utf-8')\n"
        script += emit({"event": "init", "conversation_id": "agy-progress-session", "init": {}})
        script += emit({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "Completed add; pending subtract and tests."}})
        script += emit({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "write_to_file", "tool_info": {"output": "Updated arithmetic.py"}}})
        script += "print('ERROR: API request failed HTTP 429 Too Many Requests; retrying', file=sys.stderr, flush=True)\ntime.sleep(60)\n"
        self.fake("agy", script)
        run, result = self.run_cli("agy", "Implement add and subtract in arithmetic.py; preserve the user header and verify both functions.")
        self.assert_takeover(run, result, "rate_limited")
        self.assertNotEqual(result["workerStatus"], "timeout", "Must interrupt the retry loop before the task timeout")
        self.assertEqual(result["sessionId"], "agy-progress-session")
        self.assertIn("pending subtract", result["response"])
        self.assertEqual(target.read_text(encoding="utf-8"), partial)
        self.assertIn("arithmetic.py", result["workspaceChanges"]["modified"])
        before = (self.root / "run/before.unstaged.patch").read_text(encoding="utf-8")
        after = (self.root / "run/after.unstaged.patch").read_text(encoding="utf-8")
        self.assertIn("user note", before)
        self.assertNotIn("return a + b", before)
        self.assertIn("return a + b", after)
        progress = json.loads((self.root / "run/progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["toolEvents"][0]["tool"], "write_to_file")

    def test_zcode_zero_exit_quota_keeps_response_and_new_file(self):
        script = "Path('created.txt').write_text('completed first step')\n"
        script += emit({"sessionId": "zcode-partial", "response": "First step done; second pending.",
                        "error": {"code": "insufficient_quota", "message": "Monthly quota exhausted"}})
        self.fake("zcode", script)
        run, result = self.run_cli("zcode")
        self.assert_takeover(run, result, "quota_exhausted")
        self.assertEqual(result["sessionId"], "zcode-partial")
        self.assertIn("second pending", Path(result["resultFile"]).with_name("response.md").read_text(encoding="utf-8"))
        self.assertEqual((self.work / "created.txt").read_text(), "completed first step")
        self.assertIn("created.txt", result["workspaceChanges"]["created"])

    def test_agy_terminal_error_does_not_replace_earlier_progress(self):
        script = emit({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "Completed implementation; pending verification."}})
        script += emit({"event": "result", "result": {"conversation_id": "agy-final-error", "status": "ERROR", "response": "The next request failed.", "error": "insufficient_quota"}})
        self.fake("agy", script)
        run, result = self.run_cli("agy")
        self.assert_takeover(run, result, "quota_exhausted")
        self.assertIn("Completed implementation", result["response"])
        self.assertIn("next request failed", result["response"])

    def test_opencode_error_event_keeps_partial_text(self):
        script = emit({"type": "text", "sessionID": "oc-partial", "part": {"text": "已完成检查，剩余修复待处理。"}})
        script += emit({"type": "error", "sessionID": "oc-partial", "error": {"name": "APIError", "data": {"statusCode": 429, "message": "Too Many Requests"}}})
        self.fake("opencode", script)
        run, result = self.run_cli("opencode")
        self.assert_takeover(run, result, "rate_limited")
        self.assertIn("剩余修复", result["response"])

    def test_quota_before_json_still_preserves_files_without_session_id(self):
        script = "Path('unfinished.txt').write_text('keep this work')\n"
        self.fake("zcode", script + "print('ERROR: 额度已用完，请稍后再试', file=sys.stderr)\nsys.exit(1)\n")
        run, result = self.run_cli("zcode")
        self.assert_takeover(run, result, "quota_exhausted")
        self.assertIsNone(result["sessionId"])
        self.assertEqual((self.work / "unfinished.txt").read_text(), "keep this work")

    def test_normal_429_documentation_and_tool_output_are_not_provider_errors(self):
        script = emit({"type": "text", "sessionID": "normal", "part": {"text": "Example: HTTP 429 means rate limit exceeded. This is documentation."}})
        script += emit({"type": "tool_use", "part": {"tool": "read", "state": {"status": "error", "error": {"statusCode": 429}}}})
        script += "print('Documentation example: HTTP 429 handling', file=sys.stderr)\n"
        script += "print('[INFO] x-ratelimit-remaining: 100', file=sys.stderr)\n"
        self.fake("opencode", script)
        run, result = self.run_cli("opencode")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["takeoverRequired"])

    def test_non_quota_error_does_not_claim_quota_takeover(self):
        self.fake("agy", emit({"status": "ERROR", "response": "", "error": "HTTP 401 Authentication required"}) + "sys.exit(1)\n")
        run, result = self.run_cli("agy")
        self.assertEqual(run.returncode, 1)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["takeoverRequired"])

    def test_default_receipts_are_persistent_under_codex_home(self):
        self.env["CODEX_HOME"] = str(self.root / "codex-home")
        self.fake("zcode", emit({"sessionId": "persistent", "response": "ok", "projection": {"status": "idle"}}))
        run, result = self.run_cli("zcode", output=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertTrue(Path(result["resultFile"]).is_relative_to(self.root / "codex-home/external-agent-runs"))

    def test_agy_variants_and_resume_use_the_requested_arguments(self):
        self.env["CODEX_HOME"] = str(self.root / "receipts")
        self.fake("agy", "print(json.dumps({'status':'SUCCESS', 'conversation_id':'fixture-session',"
                         " 'response':json.dumps(sys.argv[1:])}))\n")
        prompt = """Review 中文 'single' "double" $HOME & ; * (x).
Keep both lines intact."""
        cases = [
            (("--model", "gemini-4-neon-xhigh"), "gemini-4-neon-xhigh", "xhigh"),
            (("--model", "gemini-pro-orchestrator-neon"), "gemini-pro-orchestrator-neon", None),
        ]
        for flags, model, effort in cases:
            with self.subTest(flags=flags):
                run, result = self.run_cli("agy", prompt, output=False, extra=flags)
                self.assertEqual(run.returncode, 0, run.stdout)
                argv = json.loads(result["response"])
                self.assertTrue(argv[argv.index("-p") + 1].endswith("\n\n" + prompt))
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertEqual((result["model"], result["effort"]), (model, effort))
                if effort is None:
                    self.assertNotIn("--effort", argv)
                else:
                    self.assertEqual(argv[argv.index("--effort") + 1], effort)
        run, resumed = self.run_cli("agy", output=False, extra=("--resume-from", result["resultFile"]))
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(resumed["model"], result["model"])
        self.assertIsNone(resumed["effort"])
        self.assertIn("--conversation", json.loads(resumed["response"]))

    def test_conflicting_agy_effort_rejected_before_launch(self):
        for model in ("gemini-4-neon-xhigh", "gemini-pro-orchestrator-g1-pro"):
            with self.subTest(model=model):
                run = subprocess.run(self.cli_args("agy", "review", ("--model", model, "--effort", "high")),
                                     env=self.env, capture_output=True, encoding="utf-8")
                self.assertEqual(run.returncode, 2)
                self.assertIn("--effort", run.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_quota_stops_term_resistant_child_after_launcher_exits(self):
        child = ("import os, signal, sys, time\nfrom pathlib import Path\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "print('HTTP 429 Too Many Requests', file=sys.stderr, flush=True)\n"
                 "Path('child.pid').write_text(str(os.getpid()))\n"
                 "time.sleep(60)\n")
        self.fake("agy", f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                         "while not Path('child.pid').exists(): time.sleep(0.01)\n")
        run, result = self.run_cli("agy")
        self.assert_takeover(run, result, "rate_limited")
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.work / "child.pid").read_text()), 0)

    @unittest.skipUnless(os.name == "posix", "POSIX signal forwarding")
    def test_cancel_shell_entry_stops_worker_and_writes_receipt(self):
        self.fake("agy", "Path('worker.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        out = self.root / "cancel"
        with subprocess.Popen(self.cli_args("agy", "review", ("--output-dir", str(out))),
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8") as run:
            started = self.work / "worker.pid"
            try:
                deadline = time.monotonic() + 15
                while not started.exists() and time.monotonic() < deadline and run.poll() is None:
                    time.sleep(0.05)
                self.assertTrue(started.exists())
            finally:
                run.send_signal(signal.SIGTERM)
            stdout, stderr = run.communicate(timeout=15)
        self.assertEqual(run.returncode, 130, stderr)
        self.assertEqual(json.loads(stdout)["status"], "canceled")
        self.assertTrue(json.loads((out / "result.json").read_text())["workerStopped"])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(started.read_text()), 0)

    def test_timeout_stops_worker_without_quota_takeover(self):
        self.fake("opencode", "time.sleep(60)\n")
        run, result = self.run_cli("opencode", extra=("--timeout", "1"))
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["workerStopped"])
        self.assertFalse(result["takeoverRequired"])


class ModelSelectionTests(unittest.TestCase):
    def test_model_and_effort_overrides(self):
        defaults = json.loads((SCRIPTS / "profiles.json").read_text(encoding="utf-8"))["agy"]
        cases = [
            (None, None, "gemini-4-hydrogen-xhigh", "xhigh"),
            (None, "medium", "gemini-4-hydrogen-medium", "medium"),
            ("gemini-4-argon-high", None, "gemini-4-argon-high", "high"),
            ("gemini-4-hydrogen-low", None, "gemini-4-hydrogen-low", "low"),
            ("gemini-4-neon", "xhigh", "gemini-4-neon", "xhigh"),
            ("gemini-4-neon", None, "gemini-4-neon", None),
        ]
        for model, effort, expected_model, expected_effort in cases:
            with self.subTest(model=model, effort=effort):
                self.assertEqual(select_model("agy", model, effort, defaults), (expected_model, expected_effort))

    def test_profile_changes_respect_pinned_and_fixed_models(self):
        neon = {"model": "gemini-4-neon-xhigh"}
        self.assertEqual(select_model("agy", None, None, neon), (neon["model"], "xhigh"))
        fixed = {"model": "gemini-pro-orchestrator-neon", "effort": None}
        self.assertEqual(select_model("agy", None, None, fixed), (fixed["model"], None))
        with self.assertRaises(ValueError):
            select_model("agy", None, None, {**neon, "effort": "high"})


class ErrorBoundaryTests(unittest.TestCase):
    def test_partial_tool_json_does_not_expose_nested_error(self):
        partial = '{"type":"tool_use","part":{"state":{"error":{"statusCode":429}}},"rest":'
        self.assertIsNone(detect_limit(json_objects(partial), ""))

    def test_resource_exhausted_and_plain_http_stderr(self):
        signal = detect_limit([{"error": {"code": "RESOURCE_EXHAUSTED"}}], "")
        self.assertEqual(signal["kind"], "quota_exhausted")
        self.assertEqual(detect_limit([], "HTTP 429 Too Many Requests")["kind"], "rate_limited")
        self.assertEqual(detect_limit([{"error": {"code": "FREE_USAGE_LIMIT_REACHED"}}], "")["kind"], "quota_exhausted")
        self.assertEqual(detect_limit([{"error": "You have run out of credits"}], "")["kind"], "quota_exhausted")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
