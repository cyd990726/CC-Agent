# Mini Agent Runtime

[![CI](https://github.com/cyd990726/CC-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/cyd990726/CC-Agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

一个小而完整的终端 Coding Agent，用于学习和实践
`模型 → Action → Tool → Observation` 的核心工作方式。支持 OpenAI-compatible
模型、流式输出、文件操作、Shell、联网搜索和交互式权限确认。

![Mini Agent terminal preview](https://raw.githubusercontent.com/cyd990726/CC-Agent/main/docs/terminal-preview.svg)

## 三分钟上手

需要 Python 3.10 或更高版本。推荐使用
[pipx](https://pipx.pypa.io/) 隔离安装：

```bash
pipx install git+https://github.com/cyd990726/CC-Agent.git
mini-agent init
mini-agent doctor
cd your-project
mini-agent
```

也可以从源码运行：

```bash
git clone https://github.com/cyd990726/CC-Agent.git
cd CC-Agent
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .
mini-agent init
mini-agent
```

无需永久安装也可以运行：

```bash
pipx run --spec git+https://github.com/cyd990726/CC-Agent.git mini-agent
```

## 初始化与配置

运行 `mini-agent init`，按提示选择模型厂商、填写模型名与 API Key：

```text
Mini Agent 初始化

  1  OpenAI
  2  Kimi（中国大陆）
  3  Kimi（国际）
  4  DeepSeek
  5  自定义 OpenAI-compatible
```

配置默认保存到：

- macOS/Linux：`~/.config/mini-agent/.env`
- Windows：`%APPDATA%\mini-agent\.env`
- 设置 `MINI_AGENT_CONFIG` 可以指定其他位置

配置优先级为：Shell 环境变量 → 当前目录 `.env` → 用户配置。API Key 不会显示在
`doctor` 输出中，用户配置在支持权限位的系统上会设为 `0600`。

也可以复制 [.env.example](.env.example) 手动配置：

```bash
cp .env.example .env
```

必要配置：

```bash
MINI_AGENT_MODEL="your-model"
MINI_AGENT_API_KEY="your-api-key"
MINI_AGENT_BASE_URL="https://provider.example/v1"
```

常用可选配置：

```bash
MINI_AGENT_MAX_STEPS="100"
MINI_AGENT_MAX_RPM="3"
MINI_AGENT_SEARCH_PROVIDER="tavily"
TAVILY_API_KEY="your-tavily-key"
```

## 使用

进入持续交互界面：

```bash
mini-agent --workspace .
```

直接执行一个任务：

```bash
mini-agent --workspace ./my-project "修复测试并解释原因"
```

只分析并生成实施计划，不修改文件或运行 Shell 命令：

```bash
mini-agent --plan --workspace ./my-project "升级依赖并修复兼容问题"
```

检查本地配置：

```bash
mini-agent doctor
mini-agent doctor --connectivity  # 额外请求模型服务的 /models 接口
```

其他选项：

```bash
mini-agent --help
mini-agent --version
mini-agent --max-steps 100
mini-agent --no-confirm
```

交互界面支持 `/plan`、`/permissions`、`/clear`、`/status`、`/history`、`/verbose` 和
`/exit`。输入 `/` 会打开命令面板，可用 `Tab` 选择；`Shift+Tab` 可在
Ask for approval、Accept edits、Full Access 和 Plan mode 之间快速切换。
输入框下方右侧会显示当前模式；Ask for approval 状态下不显示模式标签。
长输入会自动折行并增高输入框，`Alt+Enter` 或 `Ctrl+J` 可以主动换行。`/plan` 可在
会话中切换只读计划模式并在关闭时恢复原权限；Plan mode 下通过 `/permissions`
选择权限会退出 Plan mode。

## 内置工具

| 工具 | 作用 | Ask for approval 模式下确认 |
|---|---|---|
| `read_file` | 分段读取工作区文件 | 否 |
| `edit_file` / `write_file` | 精确编辑或写入文件 | 是 |
| `find_files` / `list_files` | 查找与列出文件 | 否 |
| `search` | 搜索工作区文本 | 否 |
| `shell` | 在工作目录启动 Shell 命令 | 是 |
| `web_search` | Tavily、Brave Search 或 Serper 搜索 | 是 |
| `fetch_url` | 抓取公开网页文本 | 是 |

`web_search` 会自动选择已经配置的搜索服务，也可以通过
`MINI_AGENT_SEARCH_PROVIDER` 或 `--search-provider` 指定。`fetch_url` 不需要
API Key，并会拒绝本机、内网地址、非 HTTP(S) 协议、超大响应与二进制内容。

## 安全说明

> [!WARNING]
> Mini Agent 仍处于 Alpha 阶段，不是完整沙箱。请只在可信、隔离且已纳入版本控制的
> 工作目录中运行。

- 默认情况下，文件工具被限制在 `--workspace` 内。
- `--plan` 只开放读取、搜索和网页读取工具；文件写入、编辑和 Shell 工具不会注册。
- 默认 Ask for approval 模式会确认文件修改、Shell 和联网操作；确认卡片显示文件 diff 或完整命令。
- 联网默认需要确认。
- `shell` 虽然从 workspace 启动，但命令仍可能访问工作区外的文件和系统资源。
- `/permissions` 提供 Ask for approval、Approve for me 和 Full Access 三档。
  Approve for me 访问工作区外路径时需要确认；Full Access 允许文件工具访问工作区外路径并跳过确认。
- `--no-confirm` 会以 Full Access 启动，只应在临时容器或其他隔离环境中使用。
- 不要把 `.env`、API Key 或包含凭据的日志提交到仓库。

## 工作原理

```text
User task
    ↓
LLM returns one JSON action
    ↓
Runtime validates and executes one tool
    ↓
Observation is appended to the conversation
    ↓
Repeat until final_answer
```

模型请求使用 OpenAI-compatible Chat Completions 和 SSE 流式响应。接口发生 429
组织级 RPM 限流时，运行时会读取服务端提示并等待下一个可用窗口。

## 开发

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m build
```

CI 会在 Ubuntu、macOS 和 Windows 上测试 Python 3.10–3.14，并构建 wheel 与
source distribution。

## Roadmap

- 原生 OpenAI Responses / Anthropic tool-use adapters
- 更严格的 Shell 沙箱与安全模式
- 会话持久化与上下文压缩
- MCP 与 Skill 扩展机制
- PyPI 正式发布

## License

[MIT](LICENSE)
