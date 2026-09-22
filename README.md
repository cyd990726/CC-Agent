# Mini Agent Runtime（Phase 1）

这是一个用于理解 Coding Agent 核心机制的最小实现。当前阶段包含：

- 与具体厂商解耦的 `LLM` 接口，以及 OpenAI-compatible HTTP 适配器
- `模型 -> Action -> Tool -> Observation` Agent Loop
- 进程内任务状态
- 分段读取、精确编辑、写入、查找和列出文件、文本搜索、网页搜索与抓取、Shell 命令
- 最大执行步数、结构化协议校验和可恢复的工具错误
- 基于 SSE 的模型响应流式接收与终端答案增量输出

## 运行

需要 Python 3.10+。先安装项目依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

程序会自动读取项目根目录的 `.env`，也可以使用当前 Shell 中的环境变量
（Shell 环境变量优先）：

```bash
export MINI_AGENT_MODEL="your-model"
export MINI_AGENT_API_KEY="your-api-key"
# 可选，默认是 https://api.openai.com/v1
export MINI_AGENT_BASE_URL="https://provider.example/v1"
# 可选；账户有较低的 RPM 限制时建议主动配置
export MINI_AGENT_MAX_RPM="3"
# 可选：配置任意一个搜索服务；默认按以下顺序自动选择
export TAVILY_API_KEY="your-tavily-key"
# export BRAVE_SEARCH_API_KEY="your-brave-key"
# export SERPER_API_KEY="your-serper-key"

python main.py --workspace ./workspace/test_project "创建 hello.py 并运行它"
```

不传任务时进入持续交互界面：

```bash
python3 main.py --workspace ./workspace/test_project
```

交互界面支持 `/help`、`/clear`、`/status`、`/history`、`/verbose` 和
`/exit`。输入 `/` 会显示命令菜单，可使用 `Tab` / `Shift+Tab` 选择命令。
输入框下方会持续显示当前模型和工作目录。
长输入会自动折行并增高输入框；使用 `Alt+Enter` 或 `Ctrl+J` 可以主动换行。
`edit_file`、`write_file`、`shell`、`web_search` 与 `fetch_url` 默认会在执行前
请求确认；可以使用 `--no-confirm` 跳过确认，但只应在可信、隔离的工作目录中使用。

模型通过严格 JSON 协议选择工具或结束任务。文件工具被限制在 `--workspace`
目录内；Shell 命令在该目录中运行。Phase 1 尚未提供完整沙箱，因而只应在可信、
隔离的工作目录中运行。

`web_search` 使用可替换的搜索 Provider。默认自动选择已配置的 Tavily、Brave
Search 或 Serper，也可以通过 `MINI_AGENT_SEARCH_PROVIDER` 或
`--search-provider` 指定。`fetch_url` 不需要 API Key，支持提取公开网页文本，
并会拒绝本机、内网地址、非 HTTP(S) 协议、超大响应和不支持的二进制内容。

模型请求默认使用 OpenAI-compatible Chat Completions 的 `stream: true`。终端会
增量显示 `final_answer` 的内容，而工具调用会在完整响应通过 JSON 校验后再执行。
如果接口返回组织级 `max RPM` 限流，程序会自动识别该上限，并在后续请求前等待
可用额度；也可以通过 `MINI_AGENT_MAX_RPM` 或 `--max-rpm` 提前配置。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用假的 LLM，不会发起网络请求，也不需要 API Key。
