# Mini Agent Runtime（Phase 1）

这是一个用于理解 Coding Agent 核心机制的最小实现。当前阶段包含：

- 与具体厂商解耦的 `LLM` 接口，以及 OpenAI-compatible HTTP 适配器
- `模型 -> Action -> Tool -> Observation` Agent Loop
- 进程内任务状态
- 读取/写入文件、文本搜索、Shell 命令四个工具
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

python main.py --workspace ./workspace/test_project "创建 hello.py 并运行它"
```

不传任务时进入持续交互界面：

```bash
python3 main.py --workspace ./workspace/test_project
```

交互界面支持 `/help`、`/clear`、`/status`、`/history`、`/verbose` 和
`/exit`。`write_file` 与 `shell` 默认会在执行前请求确认；可以使用
`--no-confirm` 跳过确认，但只应在可信、隔离的工作目录中使用。

模型通过严格 JSON 协议选择工具或结束任务。文件工具被限制在 `--workspace`
目录内；Shell 命令在该目录中运行。Phase 1 尚未提供完整沙箱，因而只应在可信、
隔离的工作目录中运行。

模型请求默认使用 OpenAI-compatible Chat Completions 的 `stream: true`。终端会
增量显示 `final_answer` 的内容，而工具调用会在完整响应通过 JSON 校验后再执行。
如果接口返回组织级 `max RPM` 限流，程序会自动识别该上限，并在后续请求前等待
可用额度；也可以通过 `MINI_AGENT_MAX_RPM` 或 `--max-rpm` 提前配置。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用假的 LLM，不会发起网络请求，也不需要 API Key。
