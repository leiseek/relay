---
name: relay
description: 在 Windows、macOS 或 Linux 上通过本机 AGY、ZCode、OpenCode CLI 委派实现或独立复核任务。支持按账号选择 Gemini 模型和思考档位、续接会话，并在限流或额度耗尽时保留进度交回当前 Codex。
---

# Relay

用本技能的脚本启动用户指定的外部 CLI，复用其登录。当前 Codex 负责限定任务范围、验证结果和整合交付。

## 选择引擎和模型

| 引擎 | 默认模型 | 档位 |
| --- | --- | --- |
| AGY | `gemini-3.8-flash-high` | `high` |
| OpenCode | `opencode/deepseek-v4.1-flash-free` | `max` |
| ZCode | `account:bigmodel-individual-coding-plan/GLM-5.3-Flash` | `max` |

用户未指定模型时，沿用 [profiles.json](scripts/profiles.json) 中的偏好。只调用任务需要的引擎。

用户指定其他 Gemini 模型时，运行 `agy models` 查看当前账号的完整 ID。只指定系列时，从该系列列出的变体选最高档，顺序为 `max > xhigh > high > medium > low`。用户点名档位时按指定值选。把完整 ID 传给 `--model`，例如 `--model gemini-4-neon-xhigh`。Orchestrator 使用目录里的原始 ID，省略 `--effort`。若指定的模型或档位不可用，报告具体选择和可用档位，保留用户的选择权。[模型示例和核验命令](references/compatibility.md)

## 运行

准备 Python 3.9+，并让目标 CLI 在当前 `PATH` 中可执行。先把委派目标、允许修改的范围、交付物和验收条件写入 UTF-8 的任务文件。以下示例从项目目录读取 `review.txt`。

**Windows PowerShell**

```powershell
$skillHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$runner = Join-Path $skillHome 'skills/relay/scripts/run_agent.py'
python $runner --engine agy --cwd . --prompt-file ./review.txt
```

**macOS / Linux**

```sh
runner="${CODEX_HOME:-$HOME/.codex}/skills/relay/scripts/run_agent.sh"
sh "$runner" --engine agy --cwd . --prompt-file ./review.txt
```

两个入口共用参数。将 `--engine` 换成 `opencode` 或 `zcode` 即可调用对应 CLI。技能安装在其他位置时，使用本技能目录中的脚本。

- 默认 `--task-mode review`，按只读要求委派。原任务授权修改时使用 `--task-mode edit` 并写明文件范围。并发写入时使用独立 worktree 或互不重叠的文件范围。
- 短任务可用 `--prompt TEXT`。用 `--dry-run` 核对模型、档位和调用参数，无需提交任务。
- `--timeout` 以秒计，默认 1200。`--output-dir PATH` 指定本次结果目录，默认在 `$CODEX_HOME/external-agent-runs/` 下创建独立目录。未设置 `CODEX_HOME` 时使用 `~/.codex`。
- `--resume-from PATH/result.json` 用于追问。传入相同引擎和工作目录，模型及档位从记录继承。明确覆盖时必须与记录匹配。
- OpenCode 用 `--model provider/model --effort LEVEL`，脚本拼接变体。ZCode 用完整的账号 `provider/model`，脚本在结果目录生成本次调用的无密钥配置。

运行中沿用执行工具的会话 ID 等待，或读取输出目录的 `result.json`。成功后检查回答和实际文件改动，执行原任务要求的验证。权限模式、平台依赖和升级诊断见[兼容性与验证](references/compatibility.md)。

## 限流与接管

退出码 `75`、状态 `needs_takeover` 或 `takeoverRequired == true` 表示外部任务遇到 429 或额度耗尽。当前 Codex 接续完成该任务，保留已有成果和原授权范围。

1. 告知用户正在接手，读取 `handoffFile`、`prompt.txt`、部分 `response.md` 和 `progress.json`。需要上一轮上下文时沿 `resumeFrom` 追溯。
2. 修改同一工作区前确认 `workerStopped == true`。若为 false，先停止该工作进程。结合实际文件和运行前后的 Git 状态、diff 区分用户原有修改、代理已完成工作和待办。
3. 继续完成并验证剩余工作。只读任务继续只读。将接管结果和验证写入 `takeover.json`，保留原始 `result.json`。已有接管记录时先核实，避免重复执行。

额度耗尽后直接由当前 Codex 接手，禁止自动重试、换模型或另启 Codex CLI。独立且正常的其他任务可以继续。普通超时返回 `124`，主动取消返回 `130` 并停止。错误识别和交接文件格式见[额度恢复与进度交接](references/recovery.md)。
