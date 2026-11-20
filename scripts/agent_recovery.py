"""Quota detection and on-disk evidence for the parent Codex to continue work."""

import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def json_objects(text):
    """Decode envelopes and NDJSON, also tolerating a diagnostic prefix."""
    decoder = json.JSONDecoder()
    records = []
    # Only roots at the start of a line: a partially flushed tool event must
    # not expose its nested tool.error as if it were a provider error event.
    roots = [match.start() for match in re.finditer(r"(?m)^\{", text)]
    if not roots and "{" in text:
        roots = [text.index("{")]
    consumed = -1
    for start in roots:
        if start < consumed:
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        else:
            if isinstance(value, dict):
                records.append(value)
            consumed = end
    return records


def limit_kind(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).lower().replace("_", " ")
    exhausted = re.search(
        r"insufficient[_ -](?:quota|credits?|balance)|quota[_ -](?:exhausted|exceeded)|"
        r"(?:quota|credits?|balance|usage limit|daily limit|monthly limit).{0,60}"
        r"(?:exhausted|exceeded|depleted|used up|reached|insufficient)|"
        r"(?:exhausted|exceeded|insufficient).{0,40}(?:quota|credits?|balance)|"
        r"resource[_ -]exhausted|(?:out of|no remaining|no available|no) credits?|"
        r"额度.{0,12}(?:不足|用完|耗尽|超限)|"
        r"余额.{0,8}不足|积分.{0,8}(?:不足|用完|耗尽)", normalized)
    if exhausted:
        return "quota_exhausted"
    if re.search(r"(?<!\d)429(?!\d)|too many requests|rate[_ -]?limit(?:ed| exceeded| reached)?|请求.{0,8}(?:过多|频繁)", normalized):
        return "rate_limited"
    return None


def error_payloads(record):
    # Do not search assistant text or tool output for quota words.
    if record.get("event") == "result" and isinstance(record.get("result"), dict):
        record = record["result"]
    if record.get("error"):
        yield record["error"]
    kind = str(record.get("type") or record.get("event") or "").lower()
    state = str(record.get("status", "")).lower()
    if kind in ("error", "session.error", "turn.failed", "fatal_error") or state in ("error", "failed"):
        yield record


def detect_limit(records, stderr):
    for record in records:
        for payload in error_payloads(record):
            kind = limit_kind(payload)
            if kind:
                evidence = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                return {"kind": kind, "source": "structured_error", "evidence": evidence[:2000]}
    for line in stderr.splitlines():
        # A bare mention of HTTP 429 in ordinary prose is not an error signal.
        error_context = re.search(
            r"error|failed|retry|status(?:code|_code)?[\s\":=]|^\s*HTTP(?:/\d[.\d]*)?\s+429\b|"
            r"429\s*(?:too many requests|resource exhausted)|"
            r"quota|insufficient[_ -](?:credits?|balance)|resource[_ -]?exhausted|"
            r"额度|余额不足|积分|too many requests|"
            r"rate[_ -]?limit(?:ed|[_ -](?:exceeded|reached)|error)|"
            r"usage[_ -]limit[_ -]reached|out of credits|no (?:available |remaining )?credits", line, re.I)
        kind = limit_kind(line) if error_context else None
        if kind:
            return {"kind": kind, "source": "stderr", "evidence": line[:2000]}
    return None


class Progress:
    def __init__(self):
        self.session_id = None
        self.response = ""
        self.tools = []
        self.seen = set()
        self.steps = {}

    def consume(self, records):
        changed = False
        for original in records:
            fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).digest()
            if fingerprint in self.seen:
                continue
            self.seen.add(fingerprint)
            changed = True
            record = original
            if record.get("event") == "result" and isinstance(record.get("result"), dict):
                record = record["result"]
            self.session_id = record.get("conversation_id") or record.get("sessionId") or record.get("sessionID") or self.session_id
            if isinstance(record.get("response"), str) and record["response"]:
                response = record["response"]
                if not self.response or self.response in response:
                    self.response = response
                elif response not in self.response:
                    self.response += "\n\n" + response
            if record.get("event") == "step_update":
                step = record.get("step_update", {})
                self.session_id = step.get("conversation_id") or self.session_id
                if step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
                    index = str(step.get("step_index", 0))
                    self.steps[index] = self.steps.get(index, "") + step["text_delta"]
                    self.response = "\n".join(self.steps.values())
                if step.get("step_type") == "tool":
                    self.tools.append({"tool": step.get("tool_name"), "state": step.get("state"),
                                       "details": json.dumps(step.get("tool_info", {}), ensure_ascii=False)[:8000]})
            part = record.get("part") or {}
            if record.get("type") == "text" and isinstance(part.get("text"), str):
                self.response += ("\n" if self.response else "") + part["text"]
            if record.get("type") == "tool_use":
                self.tools.append({"tool": part.get("tool"), "state": part.get("state", {}).get("status"),
                                   "details": json.dumps(part.get("state", {}), ensure_ascii=False)[:8000]})
            self.tools = self.tools[-200:]
        return changed

    def save(self, output):
        write_json(output / "progress.json", {"sessionId": self.session_id, "response": self.response,
                                              "toolEvents": self.tools, "updatedAt": time.time(),
                                              "rawLogIsAuthoritative": True})
        temp = output / "response.md.tmp"
        temp.write_text(self.response, encoding="utf-8")
        temp.replace(output / "response.md")


class LogWatch:
    """Watch bounded tails while full, unmodified logs remain on disk."""
    def __init__(self, output, progress):
        self.output, self.progress = output, progress
        self.positions = {"stdout": 0, "stderr": 0}
        self.decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in self.positions}
        self.tails = {name: "" for name in self.positions}

    def poll(self):
        updated = False
        for name in self.positions:
            path = self.output / (name + ".log")
            if not path.exists():
                continue
            with path.open("rb") as stream:
                stream.seek(self.positions[name])
                chunk = stream.read()
                self.positions[name] = stream.tell()
            if chunk:
                updated = True
                self.tails[name] = (self.tails[name] + self.decoders[name].decode(chunk))[-2_000_000:]
        records = json_objects(self.tails["stdout"]) if updated else []
        if self.progress.consume(records):
            self.progress.save(self.output)
        return detect_limit(records, self.tails["stderr"]) if updated else None


IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".cache", "dist", "build", "target"}


def snapshot(workspace, output, label):
    """Capture observations, never stash, revert, clean, commit or rewrite files."""
    files, truncated = {}, False
    started = time.monotonic()
    for folder, dirs, names in os.walk(workspace, followlinks=False):
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS
                   and not (Path(folder) / name).is_symlink() and (Path(folder) / name).resolve() != output]
        for name in names:
            path = Path(folder) / name
            if path.is_symlink() or path.resolve().is_relative_to(output):
                continue
            try:
                info = path.stat()
                files[path.relative_to(workspace).as_posix()] = {"size": info.st_size, "mtimeNs": info.st_mtime_ns}
            except OSError:
                continue
            if len(files) >= 10000 or time.monotonic() - started > 3:
                truncated = True
                break
        if truncated:
            break
    evidence = {"cwd": str(workspace), "files": files, "inventoryTruncated": truncated,
                "inventoryExclusions": sorted(IGNORED_DIRS), "git": None}
    git = shutil.which("git")
    if git:
        prefix = [git, "--no-optional-locks", "-c", "core.quotepath=false", "-C", str(workspace)]
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            check = subprocess.run(prefix + ["rev-parse", "--show-toplevel"], capture_output=True, timeout=5, **flags)
            if check.returncode == 0:
                evidence["git"] = {"root": check.stdout.decode("utf-8", "replace").strip(), "artifacts": {}}
                commands = {"status": ["status", "--short", "--untracked-files=normal", "--", "."],
                            "unstaged": ["diff", "--no-ext-diff", "--no-textconv", "--", "."],
                            "staged": ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--", "."]}
                for kind, command in commands.items():
                    path = output / f"{label}.{kind}.{'txt' if kind == 'status' else 'patch'}"
                    with path.open("wb") as artifact:
                        run = subprocess.run(prefix + command, stdout=artifact, stderr=subprocess.DEVNULL, timeout=10, **flags)
                    evidence["git"]["artifacts"][kind] = {"path": str(path), "exitCode": run.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            evidence["snapshotWarning"] = str(exc)
    write_json(output / f"{label}.workspace.json", evidence)
    return evidence


def write_handoff(result, before, after, output):
    original, current = before.get("files", {}), after.get("files", {})
    changes = {"created": sorted(current.keys() - original.keys()),
               "modified": sorted(name for name in current.keys() & original.keys() if current[name] != original[name]),
               "deleted": sorted(original.keys() - current.keys())}
    result["workspaceChanges"] = changes
    result["workspaceInventoryTruncated"] = before.get("inventoryTruncated", True) or after.get("inventoryTruncated", True)
    handoff = output / "handoff.md"
    result["handoffFile"] = str(handoff)
    lines = ["# 外部代理进度交接", "", f"- 状态：{result['status']}",
             f"- 引擎 / 模型：{result['engine']} / {result['model']}",
             f"- 工作目录：{result['cwd']}", f"- 任务模式：{result['taskMode']}",
             f"- 会话 ID：{result.get('sessionId') or '未取得；仍可依据文件和日志接手'}",
             f"- 外部进程已停止：{result.get('workerStopped')}",
             f"- 原始任务：{output / 'prompt.txt'}", f"- 部分回答：{output / 'response.md'}",
             f"- 过程记录和工具结果：{output / 'progress.json'}",
             f"- 原始日志：{output / 'stdout.log'} / {output / 'stderr.log'}",
             f"- 启动前工作区：{output / 'before.workspace.json'}",
             f"- 停止后工作区：{output / 'after.workspace.json'}"]
    if result.get("resumeFrom"):
        lines.append(f"- 上一轮记录：{result['resumeFrom']}（必要时沿记录链补齐任务背景）")
    lines += ["", "## 失败原因", "", json.dumps(result.get("limitError") or result.get("error"), ensure_ascii=False),
              "", "## 已观察到的文件变化", "",
              "以下按运行前后文件元数据比较，可能包含并发修改；不等同于已完成或已验证。启动前的脏改动和 Git diff 另有快照。"]
    for kind, names in changes.items():
        lines.append(f"\n{kind} ({len(names)}):")
        lines.extend(f"- {name}" for name in names[:100])
        if len(names) > 100:
            lines.append("- 其余路径见 result.json 的 workspaceChanges")
    if result["workspaceInventoryTruncated"]:
        lines.append("\n文件盘点达到边界；继续检查原任务涉及的路径，不把本列表当成完整清单。")
    for label, evidence in (("启动前", before), ("停止后", after)):
        if evidence.get("git"):
            lines.append(f"\n{label} Git 证据：")
            lines.extend(f"- {kind}: {entry['path']}" for kind, entry in evidence["git"]["artifacts"].items())
    lines += ["", "## 当前 Codex 的下一步", ""]
    if result.get("takeoverRequired"):
        lines += ["由当前 Codex 接手这个子任务，沿用原任务的授权范围和验收标准。", "",
                  "1. 确认 workerStopped 为 true，再开展修改；若仍在运行，先停止这个工作进程。",
                  "2. 阅读原始任务、部分回答和工具记录，检查工作区真实文件及运行前后的 diff。",
                  "3. 保留已经有效完成的工作以及启动前已有的修改；补全尚未完成或验证的部分。",
                  "4. 按原任务运行必要验证，并把接管后的实际结果整合到主任务。",
                  "5. 另写 takeover.json 记录接管后的完成情况和验证结果，保留原始外部失败记录。", "",
                  "不要重置、清理或从头覆盖工作区；不要自动重试额度耗尽的 CLI，也不要另启一个 Codex CLI 替代当前主代理。"]
    else:
        lines.append("这些文件供诊断和人工/主代理恢复使用；本记录不要求自动重启或继续被取消的任务。")
    handoff.write_text("\n".join(lines) + "\n", encoding="utf-8")
