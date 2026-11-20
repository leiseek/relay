"""Run a local agent CLI with its own authentication and model (stdlib only)."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from agent_recovery import (LogWatch, Progress, detect_limit, json_objects,
                            snapshot, write_handoff, write_json)

SCRIPT_DIR = Path(__file__).resolve().parent
AGY_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def select_model(engine, requested_model, requested_effort, defaults):
    model = requested_model or defaults["model"]
    if engine != "agy":
        return model, requested_effort or defaults["effort"]

    family, _, variant = model.rpartition("-")
    if variant not in AGY_EFFORTS:
        variant = None
    # An explicit ID never inherits another model's configured effort.
    configured_effort = defaults.get("effort") if requested_model is None else None
    effort = requested_effort or configured_effort or variant
    if effort is not None and effort not in AGY_EFFORTS:
        raise ValueError(f"Unknown AGY effort {effort!r}. Choose from {', '.join(AGY_EFFORTS)}")
    if effort and model.startswith("gemini-pro-orchestrator"):
        raise ValueError(f"{model} has a fixed configuration. Omit --effort")
    if variant and effort and variant != effort:
        if requested_model or requested_effort is None:
            raise ValueError(f"Model {model} pins effort {variant}, which conflicts with the selected effort {effort}. "
                             "Use matching model and --effort settings")
        # --effort alone changes the variant of the configured default.
        model = f"{family}-{effort}"
    return model, effort


def cli_command(engine):
    if os.name == "nt":
        exe = shutil.which(engine + ".exe")
        if exe:
            return [exe]
        # Python's Windows which() filters .ps1 out when PATHEXT omits it.
        shim = next((str(Path(folder.strip('"')) / (engine + ".ps1"))
                     for folder in os.environ.get("PATH", "").split(os.pathsep)
                     if folder and (Path(folder.strip('"')) / (engine + ".ps1")).is_file()), None)
        if shim:
            return [shim]
        raise RuntimeError(f"Cannot find {engine}.exe or a published {engine}.ps1 shim on PATH")
    exe = shutil.which(engine)
    if not exe:
        raise RuntimeError(f"Cannot find {engine} on PATH")
    # PATH may contain relative entries. Launch happens in --cwd.
    return [str(Path(exe).absolute())]


def launch_command(command, output):
    if os.name != "nt" or Path(command[0]).suffix.lower() != ".ps1":
        return command
    powershell = shutil.which("pwsh.exe")
    if not powershell:
        raise RuntimeError("PowerShell 7 (pwsh.exe) is required for npm CLI shims")
    # JSON carries all arguments as data; the bridge also pins UTF-8 console IO.
    request = output / "invocation.json"
    write_json(request, {"executable": command[0], "arguments": command[1:]})
    return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
            str(SCRIPT_DIR / "invoke_cli.ps1"), "-RequestFile", str(request)]


def parse_result(engine, stdout, returncode, stderr=""):
    records = json_objects(stdout)
    partial = Progress()
    partial.consume(records)
    result = {"status": "error", "sessionId": partial.session_id, "response": partial.response, "usage": {}, "error": None}
    if engine in ("agy", "zcode"):
        records = [r["result"] if r.get("event") == "result" and isinstance(r.get("result"), dict) else r for r in records]
        matching = [r for r in records if "response" in r or "error" in r]
        if not matching:
            result["error"] = "CLI returned no recognized JSON result; inspect logs"
            if engine == "agy" and "[agy] print timeout" in stderr.lower():
                result.update(status="timeout", error="AGY reached its print timeout without a complete response")
            return result
        record = matching[-1]
        result.update(sessionId=record.get("conversation_id") or record.get("sessionId") or partial.session_id,
                      response=partial.response or record.get("response", ""), usage=record.get("usage", {}))
        error = record.get("error")
        native_status = str(record.get("status", "")).upper()
        projection_status = str(record.get("projection", {}).get("status", "")).lower()
        ok = (native_status == "SUCCESS" if engine == "agy" else
              not error and projection_status in ("", "idle", "completed", "success"))
        if engine == "agy" and "[agy] print timeout" in stderr.lower():
            result.update(status="timeout", error=error or "AGY reported a print timeout; output is incomplete")
        elif engine == "agy" and ok and not str(result["response"]).strip():
            result["error"] = "AGY reported success but returned no response"
        elif returncode == 0 and ok and not error:
            result["status"] = "success"
        else:
            result["error"] = error or f"CLI status={native_status or projection_status or 'unknown'}; exit={returncode}"
        return result
    texts, errors = [], []
    for record in records:
        result["sessionId"] = record.get("sessionID") or result["sessionId"]
        part = record.get("part") or {}
        if record.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
        if record.get("type") in ("error", "session.error") or record.get("error"):
            errors.append(record.get("error") or record)
        if part.get("type") == "step-finish":
            result["usage"] = part.get("tokens", result["usage"])
    result["response"] = "\n".join(texts)
    if returncode == 0 and not errors and texts:
        result["status"] = "success"
    else:
        result["error"] = errors or f"CLI exit={returncode}; no completed text response"
    return result


def worker_stopped(proc):
    if proc is None:
        return True
    if proc.poll() is None:
        return False
    if os.name == "nt":
        return True
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def terminate_tree(proc):
    if worker_stopped(proc):
        return
    if os.name != "nt":
        # A launcher can exit before a child that ignores TERM. Wait for
        # the dedicated process group, including after the leader exits.
        for sig, grace in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if worker_stopped(proc):
                    return
                time.sleep(0.05)
        return
    subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def build_args(engine, model, effort, task_mode, prompt, timeout, session, output, env):
    if engine == "agy":
        args = ["-p", prompt, "--model", model,
                "--output-format", "stream-json", "--print-timeout", f"{max(1, timeout - 15)}s",
                "--mode", "plan" if task_mode == "review" else "accept-edits"]
        if effort is not None:
            args += ["--effort", effort]
        if session:
            args += ["--conversation", session]
        return args
    if engine == "opencode":
        if "#" in model:
            raise ValueError("Pass the OpenCode model without #variant; use --effort for the variant")
        args = ["run", "--standalone", "--model", f"{model}#{effort}", "--format", "json",
                "--agent", "plan" if task_mode == "review" else "build"]
        if session:
            args += ["--session", session]
        return args + [prompt]
    if "/" not in model:
        raise ValueError("ZCode requires providerId/modelId")
    provider, model_id = model.split("/", 1)
    if not provider.startswith("account:"):
        raise ValueError("This credential-free ZCode profile requires an account provider/model")
    profile = {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {"providerRules": []},
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
            "defaultModelSelection": {"providerId": provider, "modelId": model_id,
                                      "options": {"reasoningLevel": effort}},
        },
    }
    profile_path = output / "zcode-provider.json"
    write_json(profile_path, profile)
    env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(profile_path)
    # Avoid --model: the explicit default selection carries the reasoning options.
    env.pop("ZCODE_CLI_PROMPT_MODEL", None)
    args = ["--prompt", prompt, "--json", "--no-color", "--mode",
            "guarded" if task_mode == "review" else "edit"]
    if task_mode == "review":
        args += ["--disallowed-tools", "Bash,Edit,Write,MultiEdit,NotebookEdit"]
    if session:
        args += ["--resume", session]
    return args


def main():
    def canceled(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, canceled)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, canceled)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, canceled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, choices=("agy", "zcode", "opencode"))
    parser.add_argument("--cwd", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--prompt-file", type=Path)
    parser.add_argument("--task-mode", choices=("review", "edit"), default="review")
    parser.add_argument("--model", help="Model ID (for AGY, see 'agy models')")
    parser.add_argument("--effort", help="Reasoning level. AGY model IDs with a level suffix already pin it")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    workspace = args.cwd.expanduser().resolve()
    if not workspace.is_dir():
        parser.error("--cwd must be an existing directory")
    prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8-sig")
    if not prompt.strip():
        parser.error("Prompt must not be empty")
    session = None
    if args.resume_from:
        previous = json.loads(args.resume_from.read_text(encoding="utf-8-sig"))
        for key, requested in (("engine", args.engine), ("model", args.model), ("effort", args.effort)):
            if requested is not None and requested != previous.get(key):
                parser.error(f"Resume {key} does not match the previous run")
        if Path(previous["cwd"]).resolve() != workspace:
            parser.error("Resume workspace does not match --cwd")
        if previous.get("status") in ("starting", "running", "dry_run"):
            parser.error("Cannot resume an unfinished or dry-run receipt")
        session = previous.get("sessionId")
        if not session:
            parser.error("Resume result has no sessionId")
        model, effort = previous["model"], previous["effort"]
    else:
        defaults = json.loads((SCRIPT_DIR / "profiles.json").read_text(encoding="utf-8"))[args.engine]
        try:
            model, effort = select_model(args.engine, args.model, args.effort, defaults)
        except ValueError as exc:
            parser.error(str(exc))
    if args.output_dir:
        output = args.output_dir.expanduser().resolve()
    else:
        runs = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "external-agent-runs"
        runs.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=args.engine + "-", dir=runs)).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        parser.error("--output-dir already contains result.json; use a new directory")
    mode_instruction = ("Inspect and report only. Do not modify files or execute mutating commands."
                        if args.task_mode == "review" else
                        "Perform only the changes authorized in the task below. Report changed files and validation.")
    progress_instruction = ("After each substantial step, briefly report completed actions, pending work, "
                            "and validation results before starting the next step. Preserve existing valid work.")
    delegated_prompt = f"Delegated task from Codex.\n{mode_instruction}\n{progress_instruction}\n\n{prompt}"
    result = {"engine": args.engine, "model": model, "effort": effort, "cwd": str(workspace),
              "taskMode": args.task_mode, "status": "starting", "sessionId": session,
              "response": "", "usage": {}, "error": None, "exitCode": None,
              "resultFile": str(output / "result.json"), "stdoutLog": str(output / "stdout.log"),
              "stderrLog": str(output / "stderr.log"), "elapsedSeconds": 0,
              "resumeFrom": str(args.resume_from.resolve()) if args.resume_from else None,
              "takeoverRequired": False, "nextAction": None,
              "progressFile": str(output / "progress.json")}
    proc = None
    before = None
    limit_error = None
    progress = Progress()
    watch = LogWatch(output, progress)
    started = time.monotonic()
    write_json(output / "result.json", result)
    try:
        env = os.environ.copy()
        env.update(NO_COLOR="1", PYTHONIOENCODING="utf-8")
        command = cli_command(args.engine) + build_args(
            args.engine, model, effort, args.task_mode, delegated_prompt, args.timeout,
            session, output, env)
        if args.dry_run:
            result.update(status="dry_run", command=command)
        else:
            (output / "prompt.txt").write_text(prompt, encoding="utf-8")
            before = snapshot(workspace, output, "before")
            progress.save(output)
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with (output / "stdout.log").open("wb") as stdout_log, (output / "stderr.log").open("wb") as stderr_log:
                proc = subprocess.Popen(launch_command(command, output), cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                        stdout=stdout_log, stderr=stderr_log, **flags)
                result.update(status="running", pid=proc.pid)
                write_json(output / "result.json", result)
                deadline = time.monotonic() + args.timeout
                while True:
                    limit_error = watch.poll()
                    if limit_error:
                        # Stop native retry loops before handing this workspace to the parent.
                        terminate_tree(proc)
                        break
                    if proc.poll() is not None:
                        break
                    if time.monotonic() >= deadline:
                        terminate_tree(proc)
                        result.update(status="timeout", error=f"Task exceeded {args.timeout} seconds")
                        break
                    time.sleep(0.25)
                result["exitCode"] = proc.returncode
    except KeyboardInterrupt:
        terminate_tree(proc)
        result.update(status="canceled", error="Canceled by caller")
    except Exception as exc:
        terminate_tree(proc)
        result.update(status="error", error=str(exc))
    finally:
        if result["status"] != "dry_run":
            try:
                stdout = (output / "stdout.log").read_text(encoding="utf-8", errors="replace") if (output / "stdout.log").exists() else ""
                stderr = (output / "stderr.log").read_text(encoding="utf-8", errors="replace") if (output / "stderr.log").exists() else ""
                records = json_objects(stdout)
                progress.consume(records)
                native = parse_result(args.engine, stdout, result["exitCode"], stderr)
                if result["status"] in ("starting", "running"):
                    result.update(native)
                else:
                    result.update(response=native.get("response", ""), usage=native.get("usage", {}))
                result["sessionId"] = native.get("sessionId") or progress.session_id or session
                result["response"] = result.get("response") or progress.response
                progress.session_id = result["sessionId"]
                progress.response = result["response"]
                progress.save(output)
                if not limit_error:
                    limit_error = detect_limit(records, stderr)
                    if limit_error and result["status"] != "canceled":
                        terminate_tree(proc)
                if proc is not None:
                    result["exitCode"] = proc.poll()
                result["workerStopped"] = worker_stopped(proc)
                if limit_error and result["status"] != "canceled":
                    result.update(workerStatus=result["status"], status="needs_takeover",
                                  failureKind=limit_error["kind"], limitError=limit_error,
                                  error=limit_error["evidence"], takeoverRequired=True,
                                  nextAction="continue_in_parent" if result["workerStopped"] else "stop_worker_before_takeover")
                if before is not None:
                    after = snapshot(workspace, output, "after")
                    result.update(beforeSnapshot=str(output / "before.workspace.json"),
                                  afterSnapshot=str(output / "after.workspace.json"))
                    if result["status"] != "success":
                        write_handoff(result, before, after, output)
            except Exception as exc:
                result.setdefault("recoveryWarnings", []).append(str(exc))
        result["elapsedSeconds"] = round(time.monotonic() - started, 3)
        write_json(output / "result.json", result)
        (output / "response.md").write_text(result.get("response", ""), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"success": 0, "dry_run": 0, "timeout": 124, "canceled": 130, "needs_takeover": 75}.get(result["status"], 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
