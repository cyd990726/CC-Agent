# Mini Agent Runtime（Phase 1）

这是一个用于理解 Coding Agent 核心机制的最小实现。当前阶段包含：

- 与具体厂商解耦的 `LLM` 接口，以及 OpenAI-compatible HTTP 适配器
- `模型 -> Action -> Tool -> Observation` Agent Loop
- 进程内任务状态
- 读取/写入文件、文本搜索、Shell 命令四个工具
- 最大执行步数、结构化协议校验和可恢复的工具错误

## 运行

只需 Python 3.10+，没有第三方依赖。程序会自动读取项目根目录的 `.env`，
也可以使用当前 Shell 中的环境变量（Shell 环境变量优先）：

```bash
export MINI_AGENT_MODEL="your-model"
export MINI_AGENT_API_KEY="your-api-key"
# 可选，默认是 https://api.openai.com/v1
export MINI_AGENT_BASE_URL="https://provider.example/v1"

python main.py --workspace ./workspace/test_project "创建 hello.py 并运行它"
```

模型通过严格 JSON 协议选择工具或结束任务。文件工具被限制在 `--workspace`
目录内；Shell 命令在该目录中运行。Phase 1 尚未提供完整沙箱，因而只应在可信、
隔离的工作目录中运行。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用假的 LLM，不会发起网络请求，也不需要 API Key。
