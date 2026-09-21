# Mini Agent Runtime 实现方案

## 一、项目目标

实现一个基础、简单的 Agent Runtime，用于理解 Coding Agent
的核心运行机制。

目标不是开发完整商业级 Coding Agent，而是实现一个能够：

-   接收用户任务
-   调用大语言模型
-   根据模型决策调用工具
-   在环境中执行动作
-   根据执行结果继续推理
-   直到任务完成

的最小 Agent 执行系统。

------------------------------------------------------------------------

# 二、整体架构

``` text
用户任务
    ↓
Agent Runtime

    ├── Agent Loop
    │
    ├── State 管理
    │
    ├── Context 管理
    │
    ├── Tool Executor
    │
    └── Task 判断

    ↓

Tools

    ├── File Tool
    ├── Shell Tool
    └── Search Tool

    ↓

Environment

项目代码 / 文件系统 / 操作系统
```

------------------------------------------------------------------------

# 三、项目目录设计

``` text
mini-agent-runtime/

├── main.py

├── agent/
│   ├── runtime.py
│   ├── state.py
│   └── prompt.py

├── model/
│   └── llm.py

├── tools/
│   ├── file.py
│   ├── shell.py
│   └── search.py

└── workspace/
    └── test_project/
```

------------------------------------------------------------------------

# 四、核心模块设计

## 1. State 状态管理

Agent 需要保存当前任务执行状态。

主要保存：

-   用户目标
-   对话历史
-   工具执行结果
-   当前任务是否完成

示例：

``` python
class AgentState:

    def __init__(self):
        self.messages = []
        self.current_task = ""
        self.tool_results = []
        self.finished = False
```

------------------------------------------------------------------------

## 2. Tool 工具系统

Agent 不直接操作系统，而是通过 Tool 执行动作。

基础接口：

``` python
class Tool:

    name = ""

    description = ""

    def run(self, args):
        pass
```

------------------------------------------------------------------------

### File Tool

负责：

-   读取文件
-   修改文件
-   创建文件

例如：

``` python
class ReadFileTool:

    name = "read_file"

    def run(self, path):

        with open(path) as f:
            return f.read()
```

------------------------------------------------------------------------

### Shell Tool

负责：

-   执行命令
-   运行测试
-   编译项目

例如：

``` python
subprocess.run(command)
```

------------------------------------------------------------------------

# 五、LLM 接口设计

不要绑定具体模型。

统一接口：

``` python
class LLM:

    def chat(self, messages):
        pass
```

未来可以接入：

-   OpenAI
-   Claude
-   DeepSeek
-   本地模型

模型输出建议结构化：

``` json
{
    "thought": "需要查看代码",
    "action": {
        "tool": "read_file",
        "args": {
            "path": "login.py"
        }
    }
}
```

------------------------------------------------------------------------

# 六、Agent Loop

Agent Runtime 最核心的部分。

基本流程：

``` python
while not finished:

    获取当前状态

    调用模型

    获取模型 Action

    执行 Tool

    获取 Observation

    更新 State

    判断是否完成
```

简单实现：

``` python
class AgentRuntime:

    def run(self, task):

        state = AgentState()

        while not state.finished:

            response = model.chat(
                state.messages
            )

            action = response["action"]

            result = tool.execute(action)

            state.messages.append(result)

        return state
```

------------------------------------------------------------------------

# 七、一次任务执行过程

用户：

    帮我修复登录模块的问题

执行过程：

``` text
用户任务

↓

Agent 分析

↓

读取项目文件

↓

发现代码问题

↓

执行测试

↓

获得错误信息

↓

修改代码

↓

重新测试

↓

任务完成
```

------------------------------------------------------------------------

# 八、第二阶段：增加 Memory

增加长期状态保存。

保存：

-   已查看文件
-   修改记录
-   历史任务
-   项目摘要

结构：

``` text
memory/

conversation.json

workspace_state.json
```

------------------------------------------------------------------------

# 九、第三阶段：增加 Planner

复杂任务不能直接执行，需要规划。

架构：

``` text
用户任务

↓

Planner

生成执行计划

↓

Executor

执行任务

↓

Verifier

验证结果
```

例如：

任务：

    修复支付模块 Bug

计划：

    1. 查找相关代码
    2. 分析错误
    3. 修改代码
    4. 运行测试

------------------------------------------------------------------------

# 十、第四阶段：加入 ReAct

实现：

Reason + Act

循环：

``` text
Thought

↓

Action

↓

Observation

↓

Thought

↓

Action
```

即：

思考当前问题。

选择工具。

观察结果。

继续推理。

------------------------------------------------------------------------

# 十一、第五阶段：接近 Codex / Claude Code

增加以下能力：

## 1. Repository Context

自动理解项目：

-   README
-   项目结构
-   配置文件
-   依赖关系

## 2. Context Management

解决上下文过长问题。

包括：

-   文件摘要
-   历史压缩
-   重要信息保留

## 3. Sandbox

限制危险操作。

例如：

-   删除系统文件
-   执行危险命令

## 4. Human Approval

关键操作需要人工确认。

例如：

    是否允许执行 npm install？

------------------------------------------------------------------------

# 十二、推荐学习路线

## Phase 1

目标：

实现最小 Agent。

技术：

    LLM
    +
    Tool
    +
    Agent Loop

周期：

约 1 周。

------------------------------------------------------------------------

## Phase 2

增加：

    State

    Memory

    File Tool

    Shell Tool

    Git Tool

目标：

实现简单 Coding Agent。

------------------------------------------------------------------------

## Phase 3

增加：

    Planner

    Verifier

    Self Reflection

    Context Management

目标：

接近 SWE-agent。

------------------------------------------------------------------------

## Phase 4

深入研究：

    LangGraph

    Agent RL

    SWE-bench

    SWE-Gym

------------------------------------------------------------------------

# 十三、最终能力模型

完成后，你会理解：

``` text
Model
负责推理

Agent
负责完成目标

Runtime
负责调度执行

Tool
负责操作环境

Environment
提供任务世界
```

对于 Coding Agent：

``` text
GPT / Claude

↓

Agent Runtime

↓

Shell / File / Git / Test

↓

代码仓库

↓

反馈结果

↓

继续推理
```

这也是 Codex、Claude Code、OpenHands 等系统的核心思想。
