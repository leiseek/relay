# Relay

Relay 是一个 Codex 技能，让 Codex 通过本机已登录的 AGY、ZCode 或 OpenCode 委派任务。运行过程中会保存回答、工具记录和文件改动。外部代理遇到限流或额度耗尽时，当前 Codex 可以从已完成的步骤接手。

## 安装

在 Windows、macOS 或 Linux 上安装 Python 3.9+，并把要使用的外部 CLI 加入 `PATH`。使用 skills CLI 安装到 Codex：

```sh
npx skills add leiseek/relay -g -a codex
```

## 使用

在项目的 Codex 对话中输入：

> 使用 $relay，让 AGY 用 Gemini 4 Neon 复核当前未提交的改动，列出需要修复的问题。

可以指定代理、模型和思考档位。AGY 的可用模型由当前账号决定，可运行 `agy models` 查看。运行参数、跨平台脚本示例和会话续接方式见 [技能说明](SKILL.md)。
