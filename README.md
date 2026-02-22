# R2AIBridge 客户端（Python / CLI）

这是一个面向 `radare2 + Android/Termux` 逆向场景的命令行客户端：通过 HTTP JSON-RPC（MCP 风格）连接到 [
`R2AIBridge`](https://github.com/muort521/R2AIBridge)，获取 `tools/list` 工具清单，并支持：

- `call <tool>`：手动调用远端工具（以 `tools/list` 的 schema 为准）
- `ai [--strict|--loose|--plain] <问题>`：AI 问答/分析（默认 loose 允许纯回答；strict 强制取证/最终报告门禁）
- `session`：管理 r2 会话（active/known session_id）
- `debug/config/*_reload`：运行时调参、排障与热重载
- `apk_analyze/dex_analyze/so_analyze`：固定取证流水线 + AI 深度分析

> 注意：本仓库是**客户端**。你需要先在另一端启动并可访问的 **R2AIBridge 服务端**（需提供 `/health` 与 `/mcp` 接口），客户端默认连接
`http://127.0.0.1:5050`。

## 运行环境

- Windows / Linux / macOS 均可运行
- Python 3.x
- 需要可访问的 R2AIBridge 服务端

Python 依赖（根据源码 import）：

- `requests`
- `openai`
- `rich`（可选；未安装时会自动降级为普通 stdout 输出）
- `prompt_toolkit`（可选；用于 `r2>` Tab 补全/历史；未安装时自动回退）

示例安装（仅供参考，按你的环境选择 venv/conda 等方式）：

```bash
pip install -U requests openai rich
```

可选安装（命令补全/历史）：

```bash
pip install -U prompt_toolkit
```

## 快速开始

1) 确保 R2AIBridge 服务端已启动，并能从本机访问：

- `GET {R2_BASE_URL}/health` 返回健康文本
- `POST {R2_BASE_URL}/mcp` 支持 JSON-RPC：`tools/list`、`tools/call`

2) 启动客户端：

```bash
python main.py
```

首次启动会进入交互式配置；之后会持久化到 `config.json`，下次可选择跳过。

## 配置与持久化文件

项目使用的默认持久化路径在 `lib/config.py`：

- `config.json`：运行配置（R2_BASE_URL/AI_MODEL/超时/裁剪预算/危险策略/debug 等）
- `session.json`：AI 对话会话（可选保存/下次载入）
- `kb.json`：知识库（可选写入；用于下次问题的参考注入）
- `debug.log.jsonl`：debug 事件日志（JSONL，可轮转）

### 关键配置项

可用 `config keys` 查看所有可配置项；常用项：

- `R2_BASE_URL`：R2AIBridge 地址（默认 `http://127.0.0.1:5050`）
- `MCP_TIMEOUT_S`：访问 bridge 的 HTTP 超时（秒）
- `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY` / `AI_TIMEOUT_S`
- `MAX_TOOL_RESULT_CHARS`：单次工具结果保留字符数（过长会压缩/截断）
- `MAX_CONTEXT_MESSAGES` / `MAX_CONTEXT_CHARS`：对话上下文裁剪预算
- `DEBUG_ENABLED` / `DEBUG_LOG_PATH` / `DEBUG_MAX_BYTES`
- `DANGEROUS_POLICY`：`confirm | deny | off`
- `DANGEROUS_ALLOW_REGEX` / `DANGEROUS_EXTRA_DENY_REGEX`

## 命令速查

在 CLI 中输入 `help` 会打印完整菜单。下面把 `help` 菜单里的每条命令展开说明（用途/用法/示例/注意点），方便直接照着用。

### 基础命令

- `help`
    - 用途：打印命令菜单
    - 用法：`help`

- `exit` / `quit` / `q`
    - 用途：退出客户端
    - 用法：`exit`
    - 注意：退出前会尝试保存 `config.json`；如当前 AI 有上下文，也会提示是否保存 `session.json`

### Bridge / 工具清单

- `health`
    - 用途：检查 bridge 服务健康状态（`GET {R2_BASE_URL}/health`）
    - 用法：`health`

- `list`
    - 用途：请求服务端 `tools/list`，打印原始 JSON（用于排障/确认工具是否存在）
    - 用法：`list`

- `tools`（以及兼容别名 `local_tools`）
    - 用途：打印当前加载到本地的 tool schema（以最近一次成功的 `tools/list` 为准）
    - 用法：`tools`
    - 注意：如果你刚刚切换了 bridge 或服务端更新了工具，建议先 `bridge_reload`

### status / self_check（推荐先用这两个排障）

- `status`
    - 用途：打印当前状态汇总（bridge/schema/AI/session/debug）
    - 用法：`status`

- `self_check`
    - 用途：一次性自检（本机 python、bridge health、tools/list、AI key 是否配置、是否启用 AI）
    - 用法：`self_check`
    - 注意：这是 best-effort，自检失败不会中断程序；但会给出 FAIL 原因

### 手动调用工具：call

```text
call <工具名> [JSON参数]
```

- 用途：直接调用服务端 `tools/call`（完全按 `tools/list` 的 `inputSchema` 校验）
- 参数规则：
    - 不带 JSON 时等价于 `{}`，例如：`call r2_test`
    - JSON 必须是对象（dict），否则报错
    - 不允许包含 schema 未定义的字段（会被 `validate_args` 拒绝）
    - required 字段必须齐全；required 且类型为 string 的字段不能为空字符串
- `session_id` 自动补齐：
    - 若工具 schema 的 required 包含 `session_id` 且你没传
    - 且当前存在 `active_session_id`
    - 则会自动补齐 `args["session_id"]=active_session_id`

示例：

```text
call r2_test
call r2_open_file {"file_path":"/storage/emulated/0/a.so","auto_analyze":false}
call r2_run_command {"session_id":"session_xxx","command":"afl"}
```

### AI 自动分析：ai / ai_reset / ai_reload / bridge_reload

- `ai [--strict|--loose|--plain] <问题>`
    - 用途：AI 问答/分析（可能自动 tool_calls）
    - 模式说明：
        - `--loose`（默认）：允许按用户意图直接回答；只有需要取证时才会 tool_calls；不会因为“缺少最终 Markdown”而强制重试
        - `--plain`：等同 `--loose`（语义更直观：纯回答）
        - `--strict`：强制取证与最终报告门禁（必须 tool_calls 或最终 Markdown：`## 关键发现/## 证据来源/## 下一步建议`）
    - 用法示例：
        - `ai --tools`（推荐：列工具不走 AI）
        - `ai --plain 查询可用工具，不做任何分析，只回复功能列表`
        - `ai --strict 先打开 /storage/.../a.so 并列出导入导出`
    - 行为：
        - 可能注入知识库参考（如命中）
        - 结束后会提示是否将最终结果写入知识库（写入前会检测 DSML）
        - 可选择继续上一轮 AI 分析（继续时会默认使用 strict 以避免半成品）

- `ai_reset`
    - 用途：清空 AI 对话上下文
    - 用法：`ai_reset`

- `ai_reload [keep|reset]`
    - 用途：重新初始化 AIAnalyzer，让 `AI_BASE_URL/AI_MODEL/AI_API_KEY/AI_TIMEOUT_S` 等配置立即生效
    - 用法：`ai_reload keep` / `ai_reload reset`
    - 说明：
        - `keep`：尽量保留旧的对话上下文与 session_ids
        - `reset`：不保留旧上下文（等价于重新开始）

- `bridge_reload`
    - 用途：重连 bridge，并重新 `tools/list` 刷新 schema（让 `R2_BASE_URL/MCP_TIMEOUT_S` 等配置立即生效）
    - 用法：`bridge_reload`
    - 注意：如果 schema 变化，建议紧接着执行一次 `ai_reload`，让 system prompt 里的工具清单同步更新

### session 管理（r2 session_id）

- `session list`
    - 用途：列出已知 session（包含 `known_sessions`，以及 analyzer 内部记录的 session_ids）
    - 用法：`session list`

- `session use <session_id>`
    - 用途：设置当前 `active_session_id`
    - 用法：`session use session_...`

- `session close <id|active|all>`
    - 用途：调用 `r2_close_session` 关闭会话，并同步清理本地记录
    - 用法：`session close active` / `session close all` / `session close session_...`
    - 注意：关闭过程中 `Ctrl+C` 会中断剩余关闭操作

### debug（JSONL 事件日志）

- `debug`
    - 用途：查看当前 debug 状态、log path、轮转阈值
    - 用法：`debug`

- `debug on [path]` / `debug off`
    - 用途：启用/关闭 debug 日志（并保存到 `config.json`）
    - 用法：`debug on`、`debug on ./debug.jsonl`、`debug off`

- `debug path <path>`
    - 用途：设置 debug 日志路径（并保存到 `config.json`）
    - 用法：`debug path ./debug.log.jsonl`

- `debug max_bytes <n>`
    - 用途：设置 debug 日志轮转阈值（字节），`0` 表示关闭轮转
    - 用法：`debug max_bytes 10485760`

- `debug tail [n]`
    - 用途：查看最近 n 条 debug 事件（默认 30）
    - 用法：`debug tail`、`debug tail 100`

- `debug trace <trace_id> [n]`
    - 用途：按 trace_id 查看该次会话的事件链（默认 200）
    - 用法：`debug trace <trace_id>`、`debug trace <trace_id> 500`

- `debug export <trace_id|last> [out_dir]`
    - 用途：导出排障包：`trace.jsonl + config.json + status.json + README.txt`
    - 用法：`debug export last`、`debug export <trace_id> ./exports`
    - 建议：遇到“AI 中断/工具异常/400 messages tool 关联错误”等问题时，优先导出给排查用

### config（运行时修改配置）

- `config keys`
    - 用途：列出所有可配置项
    - 用法：`config keys`

- `config show`
    - 用途：打印当前配置（`AI_API_KEY` 会脱敏）
    - 用法：`config show`

- `config set <key> <value>`
    - 用途：修改配置并保存到 `config.json`；部分字段会热更新
    - 用法：`config set MCP_TIMEOUT_S 45`
    - 热更新说明（大致）：
        - debug 相关项：立即生效
        - `MCP_TIMEOUT_S`：尝试直接更新 `bridge.timeout`
        - AI 裁剪预算项：如 AI 已启用，会直接更新 analyzer 对应属性
        - AI 连接/模型项：保存后需 `ai_reload`
        - `R2_BASE_URL`：保存后需 `bridge_reload`

## 工作流命令（固定取证 + AI 深入）

这些命令会先跑一段固定取证（tool 调用），再交给 AI 输出最终 Markdown：

- `apk_analyze [--fast|--deep] <apk_path>`
- `dex_analyze [--fast|--deep] <dex_path>`
- `so_analyze  [--fast|--deep] <so_path>`

示例：

```text
apk_analyze --fast /storage/emulated/0/Download/app.apk
so_analyze --deep /storage/emulated/0/Download/libxxx.so
```

> 备注：路径通常应是 Android/Termux 端可访问的绝对路径（例如 `/storage/...`）。如果你在 Windows 里输入了 `C:\...`，CLI 会提示你改用
> Android 路径。

## 危险命令策略（termux_command）

`call termux_command` 可能执行高风险 shell 操作。本项目提供策略开关：

- `DANGEROUS_POLICY=confirm`（默认）：检测到高风险命令会二次确认
- `DANGEROUS_POLICY=deny`：直接阻止（可用 `--force` 强制）
- `DANGEROUS_POLICY=off`：关闭检测

并支持正则覆盖：

- `DANGEROUS_ALLOW_REGEX`：白名单放行
- `DANGEROUS_EXTRA_DENY_REGEX`：额外黑名单

强制执行示例：

```text
call termux_command --force {"command":"rm -rf /data/local/tmp/some_dir"}
```

## 运行测试（离线）

测试使用 `unittest`，并通过 dummy client 避免真实联网：

```bash
python -m unittest -v
```

## 常见问题

- `tools/list` 获取失败：先确认 `{R2_BASE_URL}/health` 可访问，再检查服务端是否支持 `/mcp` 与 JSON-RPC 方法
- `ai` 不可用：检查 `AI_API_KEY` 是否为空；可用 `config show` 查看（会脱敏）
- schema 不一致：执行 `bridge_reload` 刷新 `tools/list`；再执行 `ai_reload` 让 system prompt 注入最新工具清单

## 许可证

本项目使用 R2AIBridge，遵循 LGPL-3.0 许可证。

## 贡献

欢迎提交 Issue 和 Pull Request！