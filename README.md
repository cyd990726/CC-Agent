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

配置优先级从高到低为：命令行参数 → Shell 环境变量 → 项目 `.env` →
项目 `mini-agent.toml` → 用户 `.env` → 用户 `config.toml`。API Key 不会显示在
`doctor` 输出中，用户配置在支持权限位的系统上会设为 `0600`。

### TOML 配置文件

除 `.env` 外，也可以用 TOML 配置文件（参考
[mini-agent.example.toml](mini-agent.example.toml)）：

- 项目配置：从当前目录向上查找最近的 `mini-agent.toml` 或
  `.mini-agent.toml`，因此在子目录中运行也会命中项目配置
- 用户配置：`~/.config/mini-agent/config.toml`
  （Windows 为 `%APPDATA%\mini-agent\config.toml`），可用 `MINI_AGENT_SETTINGS`
  指定其他位置

```toml
model = "bonsai2-27b"
base_url = "http://127.0.0.1:8080/v1"
api_key = "your-api-key"
max_steps = 20
sandbox = false

[search]
provider = "tavily"
tavily_api_key = "your-tavily-key"
```

键名写错、类型不对或值为空时会直接报错，并指出文件和键名。
`mini-agent doctor` 会按优先级列出实际加载的配置文件。

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

可选 Shell sandbox：

```bash
MINI_AGENT_SANDBOX="1"
```

Linux 需要安装 `bubblewrap`；macOS 会尝试使用系统 `sandbox-exec`。启用后，
Linux 上的 `shell` 命令默认只能写入 workspace 和 Mini Agent 临时目录；
shell 命令默认不能出网。macOS 当前仅提供网络隔离 MVP，
不承诺完整文件系统隔离。`mini-agent doctor` 会显示 sandbox 可用性。

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
`/exit`。也支持 `/new` 开始新会话、`/resume` 列出历史会话并用方向键选择、
`/resume <session-id>` 直接恢复指定会话并回放该会话历史、
`/session` 查看当前会话、`/sessions`
列出最近会话、`/memories` 查看项目记忆、`/remember ...` 保存一条项目记忆。
输入 `/` 会打开命令面板，可用 `Tab` 选择；`Shift+Tab` 可在
Ask for approval、Accept edits、Full Access 和 Plan mode 之间快速切换。
`Ctrl+O` 可展开或折叠最近的工具结果，`Ctrl+End` 返回最新输出。
输入框下方右侧会显示当前模式；Ask for approval 状态下不显示模式标签。
长输入会自动折行并增高输入框，`Alt+Enter` 或 `Ctrl+J` 可以主动换行。`/plan` 可在
会话中切换只读计划模式并在关闭时恢复原权限；Plan mode 下通过 `/permissions`
选择权限会退出 Plan mode。

任务执行期间仍可输入，后续任务会按顺序排队。滚轮或 `PageUp/PageDown` 浏览记录，
`Ctrl+End` 返回最新内容。拖动选择输出后松手会自动复制，`Ctrl+C` 可再次复制，
`Esc` 清除选区；没有选区时，`Ctrl+C` 取消当前任务并保留输入草稿。
选择期间输出视图固定，清除选区后恢复更新；调整窗口宽度会清除旧选区。
本地剪贴板不可用时会请求终端复制，是否成功取决于终端的 OSC 52 支持。

Mini Agent 会把会话和项目记忆保存到用户数据目录，默认是
`~/.local/share/mini-agent/projects/<project>/`，也可以用 `MINI_AGENT_HOME`
指定根目录。交互模式默认准备一个新会话，但不会保存未执行过任务的空会话；使用
`--resume` / `--resume <session-id>` 启动时恢复已有会话，也可以在交互界面用
`/resume` 选择历史会话。
第一条任务提交后会话才正式落盘，单次任务也会保存为持久会话。每个会话存放在
`sessions/<session-id>/`：`meta.json` 保存标题和更新时间等轻量信息，
`events.jsonl` 追加记录任务、模型响应、工具结果和终止状态，`snapshot.json`
作为长会话恢复加速缓存。项目记忆存放在该目录的 `memory/MEMORY.md`，
会注入后续任务的系统提示；Mini Agent 会主动保存适合跨会话保留的用户偏好、
更正、决策和项目背景，不会为写入自己的记忆文件额外弹权限确认。
会话记录会在任务提交时立即保存标题和任务项，并在工具调用完成、最终回答、
失败或取消事件发生时继续 checkpoint 当前 turn；中途退出后用 `/resume`
至少能看到任务执行到的状态和已完成工具，继续提问时也能读取已 checkpoint 的
`messages` 上下文。

授权确认展示完整命令或代码差异，支持方向键和 Enter，也可用 `1/2/3` 选择
允许一次、会话内允许该工具、拒绝；默认选中拒绝。会话授权适用于该工具后续的
所有调用，并非只授权当前命令或文件。工具结果默认显示摘要，正文最多预览 3 行、
600 字符；编辑结果默认折叠 diff，失败的 shell 结果优先预览 stderr。
`Ctrl+O` 展开或折叠单个结果，`/verbose` 切换全部工具结果（包括已有历史）的
展开状态。模型协议重试会直接显示错误原因，无需开启 verbose。

## 内置工具

| 工具 | 作用 | Ask for approval 模式下确认 |
|---|---|---|
| `read_file` | 分段读取工作区文件 | 否 |
| `edit_file` / `write_file` | 精确编辑或写入文件 | 是 |
| `find_files` / `list_files` | 查找与列出文件 | 否 |
| `search` | 搜索工作区文本 | 否 |
| `read_memory` | 读取项目记忆 | 否 |
| `remember` | 保存项目记忆 | 否 |
| `shell` | 在工作目录启动 Shell 命令 | 是 |
| `web_search` | Tavily、Brave Search 或 Serper 搜索 | 是 |
| `fetch_url` | 抓取公开网页文本 | 是 |

`web_search` 会自动选择已经配置的搜索服务，也可以通过
`MINI_AGENT_SEARCH_PROVIDER` 或 `--search-provider` 指定。`fetch_url` 不需要
API Key，并会拒绝本机、内网地址、非 HTTP(S) 协议、超大响应与二进制内容。

## 安全说明

> [!WARNING]
> Mini Agent 仍处于 Alpha 阶段。Shell sandbox 默认关闭，启用前请先运行
> `mini-agent doctor` 检查本机依赖；未启用时，请只在可信、隔离且已纳入版本控制的
> 工作目录中运行。

- 默认情况下，文件工具被限制在 `--workspace` 内。
- `--plan` 只开放读取、搜索和网页读取工具；文件写入、编辑和 Shell 工具不会注册。
- 默认 Ask for approval 模式会确认文件修改、Shell 和联网操作；确认卡片显示文件 diff 或完整命令。
- 联网默认需要确认。
- `shell` 默认从 workspace 启动；启用 `MINI_AGENT_SANDBOX=1` 后，会先通过
  Linux `bubblewrap` 或 macOS `sandbox-exec` 包装再执行。
- `/permissions` 提供 Ask for approval、Approve for me 和 Full Access 三档。
  Approve for me 访问工作区外路径时需要确认；Full Access 允许文件工具访问工作区外路径并跳过确认。
- Full Access 不会自动关闭已启用的 Shell sandbox。
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
