# 额度恢复与进度交接

## 接管信号

```json
{
  "status": "needs_takeover",
  "failureKind": "quota_exhausted",
  "takeoverRequired": true,
  "nextAction": "continue_in_parent",
  "workerStopped": true,
  "handoffFile": ".../handoff.md"
}
```

退出码为 75。脚本负责识别、停止外部 CLI 并保留资料；继续完成任务的是正在使用本 Skill 的当前 Codex。普通终端中单独运行脚本只生成交接资料，不会自行启动另一个模型。

`rate_limited` 表示 429 / Too Many Requests / rate limit；它不一定代表总额度耗尽。`quota_exhausted` 表示 insufficient_quota、RESOURCE_EXHAUSTED、额度/积分/余额耗尽等实际错误。两种情况都交回当前 Codex，避免让外部 CLI 持续重试。

识别范围是结构化错误事件和 stderr 错误日志。正常回答、文档、源代码或工具输出中提及 429，不作为模型限额错误。401、503 等其他错误仍按普通错误处理，除非同时提供明确的额度耗尽信号。

## 保留的进度

默认运行记录持久保存在 `~/.codex/external-agent-runs/`（尊重 CODEX_HOME），也支持显式指定输出目录。

| 文件 | 用途 |
| --- | --- |
| `prompt.txt` | 当前轮委派任务；result.json 的 resumeFrom 可追溯前一轮 |
| `response.md` | 已收到的公开回答/进度文本，失败的空回答不会覆盖先前片段 |
| `progress.json` | 已收到的会话 ID、回答和最近的工具调用/结果 |
| `stdout.log` / `stderr.log` | 完整原始输出，摘要不足时按需读取 |
| `before.workspace.json` / `after.workspace.json` | 运行前后文件大小/修改时间的盘点 |
| `before.*.patch` / `after.*.patch` | 可用时保留 Git 暂存和未暂存 diff |
| `before.status.txt` / `after.status.txt` | 可用时保留 Git 状态，区分原有脏改动 |
| `handoff.md` | 失败原因、原任务和证据路径、观察到的变化及接管步骤 |
| `takeover.json` | 当前 Codex 接手后写入的完成/受阻状态、补完内容和验证结果 |

工作区文件保持原样。新文件、二进制内容及未提交改动以工作区为准；快照不替代工作区，也不自动应用补丁。文件盘点有时间/数量边界，并排除常见依赖和构建目录；`workspaceInventoryTruncated` 为 true 时需按原任务继续检查相关路径。并发文件变化不能仅凭快照归因于某一个代理。

AGY 使用官方 `stream-json`，从 init / step_update / result 提前回收会话、部分回答和工具结果。OpenCode 保存 NDJSON text / tool_use / error。ZCode 继续使用其公开 JSON 接口；如果进程未输出中间内容，接管时依据已有文件、日志和已取得的会话信息重建进度，不把空回答等同于“什么也没做”。没有导出的进程内上下文不能凭空恢复。

## 接手后的操作

先停止受阻工作进程，读任务和交接记录，再检查实际改动。对有效的已完成步骤直接沿用；对不完整文件或失败验证进行补完。保留用户原有修改。原任务允许修改时才修改，原任务只读时继续只读。验收后由主任务给出最终交付，并另写 `takeover.json` 记录 `status`（completed / blocked）、`actor: parent_codex`、实际补完内容和验证结果；不把原始外部失败记录改成成功。再次读到同一交接时先检查该完成记录。

遇到普通错误/超时也保存交接资料，但不将用户主动取消视为自动续做授权。

## 文档依据与回归测试

- [AGY 流式事件](https://www.antigravity.google/docs/cli/headless/#streaming-json)
- [OpenCode v2 CLI](https://dev.opencode.ai/v2/docs/cli/commands/)
- 本技能的 `tests/test_recovery.py` 使用本地模拟 CLI 重现中途写文件后 429、额度耗尽、正常回答提及 429 等情况，不请求真实模型。
