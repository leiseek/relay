# 兼容性与验证

## 运行环境

三个平台共用 Python 3.9+ 标准库。macOS 和 Linux 使用 `sh scripts/run_agent.sh` 或 `python3 scripts/run_agent.py`，从当前 `PATH` 查找可执行文件，可直接使用 Homebrew、npm 等安装方式提供的符号链接。Windows 使用 `python scripts/run_agent.py`，优先查找原生 `.exe`。通过 npm 的 `.ps1` 启动时需要 PowerShell 7。安装路径有空格时给路径加引号。

macOS 和 Linux 上，每次实际调用都有独立进程组。限流、超时或取消时，脚本终止该组，并在交接记录中报告是否已停止。同一工作区的接管以 `workerStopped` 为准。

## 模型与思考档位

2026-11-20 在 Windows 上用 AGY 1.5.13 的 `agy models` 核对了下表。账号和版本决定实际可用项。升级后直接重查目录，新变体可以用完整 ID 传入脚本。

| Gemini 系列 | 当前账号列出的档位 | 本机最高档或固定配置的 ID |
| --- | --- | --- |
| Gemini 4 Neon | low / medium / high / xhigh | `gemini-4-neon-xhigh` |
| Gemini 4 Argon | low / medium / high | `gemini-4-argon-high` |
| Gemini 4 Hydrogen | low / medium / high / xhigh | `gemini-4-hydrogen-xhigh` |
| Gemini 3.8 Flash | low / medium / high | `gemini-3.8-flash-high` |
| Orchestrator (Neon) | 固定配置 | `gemini-pro-orchestrator-neon` |
| Orchestrator (Argon) | 固定配置 | `gemini-pro-orchestrator-g1-pro` |

需要核实当前档位时，运行 AGY 的内置命令。例如：

```sh
agy --model gemini-4-neon-xhigh -p '/effort' --output-format json
```

读结果的 `command.data`。Neon 返回 `current: xhigh` 及可用档位，固定配置返回 `adjustable: false`。带档位后缀的模型 ID 已经确定档位，额外的 `--effort` 必须与它一致。直接传无档位后缀的模型时，省略 `--effort` 会采用 AGY 原生配置，运行记录中的 `effort` 为 `null`。如需把 Orchestrator 设为长期默认，在 `profiles.json` 中填入对应 ID 并将 `effort` 设为 `null`。

以下为 2026-10-20 核对的 OpenCode 2.0.32 和 zcode-app-cli 3.15.3-37 / runtime 0.16.9 行为。

- OpenCode：`opencode api model.list` 的 `data` 数组中，`providerID == opencode`、`id == deepseek-v4.1-flash-free` 的变体为 `low/high/max`。用 `--model opencode/deepseek-v4.1-flash-free#max`。目录可能含其他供应商配置，不要打印或持久化整个目录；仅提取目标模型的 ID 和变体。
- ZCode：已安装的官方 provider catalog 为 GLM-5.3-Flash 声明 `low/high/max`。公开 provider 配置的 `config.defaultModelSelection.options.reasoningLevel` 支持 `max`。单次配置选择 `account:bigmodel-individual-coding-plan/GLM-5.3-Flash`，空的个人 provider 规则不影响原生账号登录。配置不含 API 密钥，也不覆盖日常默认配置。

## 结果和权限

- AGY 的失败结果可能退出 0；检查 JSON `status`、`error` 和 stderr。实测 `--print-timeout` 超时后甚至可能返回 `status: SUCCESS`、空回答、零用量；stderr 的 `[agy] print timeout` 必须覆盖该状态并记为 timeout，空回答也不能当成功。脚本给 CLI 启动和原生超时回收预留 15 秒。Gemini 3.8 Flash 可能暂时出现 503 capacity 错误或无回答超时；报告错误，用户未要求时不换 Sonnet 或其他模型。
- ZCode 帮助列出的 `--mode plan` 在当前运行时会报不支持。review 使用 `guarded` 并禁用写入/终端工具；edit 使用 `edit`。不要用其 headless 默认 `yolo`。
- OpenCode `--format json` 返回 NDJSON 事件。收集 `type=text` 的 `part.text`、`sessionID` 和 error；不要把完整流当单个 JSON。`--standalone` 让每次运行使用自己的进程，便于隔离和超时清理。
- 三者的 review 模式都不是操作系统沙箱。对需要隔离的任务，使用适当工作目录或 Git worktree；不把提示词约束描述成强制文件系统边界。

## 升级后的诊断

先运行各 CLI 的 `--help` 和模型目录，再用空目录做不读文件、不调用工具的固定回复测试。ZCode 单次配置的模型和档位可通过该会话的公开会话信息或只读检查本地 `runtime/model_selection` 记录核验；不要依赖模型自己声称的名称。

从技能目录运行离线回归测试，macOS / Linux 使用 `python3 -m unittest discover -s tests -v`，Windows 使用 `python -m unittest discover -s tests -v`。测试用模拟 CLI 检查参数传递、会话续接、额度交接和平台进程清理。

2026-11-20 验证结果：Alpine Linux（Python 3.12）通过全部 17 项；Windows（Python 3.14）通过 15 项，跳过两项 POSIX 专用测试。macOS 共用已在 Linux 测试的 shell 入口及进程组逻辑，尚未实机验证。

本机 ZCode 使用 BigModel 原生账号。若用户要改用 Z.AI 原生账号，确认该账号可用后，把 `profiles.json` 的 provider 改为 `account:zai-individual-coding-plan`。脚本对自定义 API-key provider 报错，避免复制密钥配置或静默选用其他模型；该需求出现时再扩展。

## 一手文档

- [AGY headless](https://www.antigravity.google/docs/cli/headless/)
- [Google 的模型与 Orchestrator 说明](https://antigravity.google/docs/models)
- [AGY effort](https://www.antigravity.google/docs/cli/commands/effort/)
- [OpenCode v2 models](https://dev.opencode.ai/v2/docs/models/)
- [ZCode CLI provider 配置](https://github.com/kingsword09/zcode-cli/blob/main/docs/PROVIDER_CONFIG.md)
- [ZCode CLI host contract](https://github.com/kingsword09/zcode-cli/blob/main/docs/HOST_INTEGRATION.md)
