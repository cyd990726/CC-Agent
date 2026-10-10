# Mini-Agent 交互体验审计与 TurnViewModel 设计

状态：设计提案

日期：2026-10-10

当前实施状态：阶段 A/B 的基础切片已开始。仓库已加入共享 Presentation 数据模型、
TerminalViewState、两个 Reducer、旧事件适配器、ToolPresenterRegistry 和可重放事件
fixture。现有 Renderer 暂时继续负责实际输出，同时以 shadow state 方式构建新的
SessionPresentation。阶段 C 已将工具请求和结果摘要委托给 ToolPresenterRegistry；
回答 delta、commentary 和完成信息也已改为消费归一化 PresentationEvent，不再由
Renderer 解析模型 JSON。TranscriptBlock 已支持稳定 ID、compact/expanded 投影和
原位更新，prompt、回答、commentary、普通工具结果和探索工具结果均已接入。任务
队列和交互式权限生命周期已进入 SessionPresentation；终端本地维护焦点、展开、
滚动和未读状态。

## 1. 结论

Mini-Agent 当前的交互问题不是“功能太少”，也不是 Python 本身造成的。
现有实现已经覆盖流式回答、任务排队、权限确认、取消、历史滚动、窗口重排、
文本选择和紧凑工具摘要，但显示状态分散在 `TerminalApp` 与
`TerminalRenderer` 中，Runtime 的底层事件会直接触发绘制副作用。随着交互能力
增加，这种结构让界面越来越难形成稳定、连贯的用户心智模型。

本设计建议：

1. 保留 Python Runtime、`prompt_toolkit` 和 Rich，不做跨语言重写。
2. 在 `AgentEvent` 与终端 Renderer 之间增加平台无关的 `TurnViewModel`，并把
   共享展示状态与终端本地状态分开。
3. Transcript 保存语义块，不再把 Rich 渲染对象或 ANSI 文本当作界面真相源。
4. 工具调用通过稳定 `call_id` 关联请求、授权、执行和结果。
5. 默认界面只展示任务、关键操作、异常和答案；详细参数与长输出按需展开。
6. 先完成一条可回归验证的单任务链路，再迁移权限、队列和长会话。

目标架构：

```text
Terminal input ──> TerminalController
                         ├── local UiAction ──> reduce_terminal()
                         │                            │
                         │                     TerminalViewState
                         │
                         └── AgentCommand ──> SessionController / Runtime
                                                     │
AgentRuntime ── AgentEvent ──────────┐                │
PermissionHandler ─ PermissionEvent ─┤<───────────────┘
                                     ▼
                           PresentationAdapter
                                     │
                                     ▼
                         reduce_presentation()
                                     │
                                     ▼
                         SessionPresentation
                         ├── TurnViewModel[]
                         ├── pending permissions
                         └── queued turn ids
                                     │
                      ┌──────────────┴──────────────┐
                      ▼                             ▼
    TerminalViewState + Renderer              future client
```

## 2. 审计范围与方法

本次审计覆盖：

- `agent/runtime.py`：事件顺序、流式输出、工具调用和完成语义。
- `agent/events.py`：当前运行时事件契约。
- `ui/terminal.py`：输入、队列、滚动、审批、快捷键和退出生命周期。
- `ui/renderer.py`：流式解析、临时状态、工具摘要和稳定输出。
- `ui/transcript.py`：宽度相关的 Rich 快照缓存。
- `ui/permissions.py`：权限判断、预览与交互入口。
- `ui/selection.py`：历史文本选择和复制。
- `tests/test_ui.py`、`tests/test_ui_interactions.py`、
  `tests/test_selection.py`、`tests/test_cancellation.py`：当前行为契约。
- `/Users/bytedance/Desktop/claude-code` 中与全屏布局、消息行、工具展示、
  权限请求和虚拟滚动直接相关的实现。

这是一份代码和交互架构审计，不包含本轮行为修改。视觉判断以现有终端预览、
代码路径和测试所表达的交互约定为依据；最终视觉效果仍需要真实终端人工验收。

## 3. 当前交互架构

### 3.1 当前数据流

```text
用户输入
  │
  ├─ 斜杠命令 ───────────────> TerminalApp 直接修改 UI / Runtime 模式
  │
  └─ 普通任务 ─> task queue ─> worker thread
                                  │
                                  ▼
                            AgentRuntime.run()
                                  │
                                  ▼
                              AgentEvent
                                  │
                                  ▼
                          TerminalRenderer
                          ├── 修改内部临时状态
                          ├── 生成 Rich 对象
                          ├── 更新 activity 回调
                          └── 写入 transcript 回调
                                  │
                                  ▼
                            TerminalApp
                            ├── 保存 Rich 快照
                            ├── 按宽度重新渲染
                            ├── 管理滚动锚点
                            └── 刷新 prompt_toolkit
```

### 3.2 当前实现值得保留的部分

当前实现并不是简单的 `print()` 循环，以下能力已经有明确测试，应在重构中保留：

- 单个 `prompt_toolkit.Application` 独占交互终端，避免多个输入循环争抢 TTY。
- Runtime 通过事件回调与 UI 解耦。
- 后台 worker 运行任务，输入区在任务执行期间仍可用，新任务按顺序排队。
- Activity 与 footer 分行，任务状态不会覆盖模型、目录和权限模式。
- 历史滚动按屏幕行处理；离开底部后，新输出不会强制抢走用户位置。
- Markdown、表格和工具记录能在窗口宽度变化时重新排版。
- 权限等待由终端线程响应，完整命令或 diff 保留在可滚动区域。
- `Ctrl+C` 会按“复制选区、取消任务、清空输入”的上下文优先级工作。
- 取消可传播到模型等待和 Shell 进程组，并阻止迟到的流式内容污染下一任务。
- 中文、emoji、JSON Unicode 转义跨 chunk 等边界已有回归测试。

这些都是可复用资产。新版设计的目的不是推翻它们，而是给它们增加统一的状态
来源，让后续修改不再依赖多个对象之间的隐式约定。

## 4. 体验问题与工程根因

### P0：显示状态没有唯一真相源

当前至少有三份互相关联的状态：

- `TerminalRenderer` 保存回答流、当前工具、探索工具组和计时状态。
- `TerminalApp` 保存 transcript、activity、权限 overlay、队列和滚动状态。
- `TranscriptBlock` 保存已经深复制的 Rich 对象及当前宽度的渲染缓存。

一次事件可能先修改 Renderer，再通过一个或多个回调修改 TerminalApp。界面是否
正确依赖调用顺序和临时布尔值，例如 `_tool_request_printed`、
`_streamed_answer`、`_has_answer_stream`、`_answer_blocks_written`。这种状态适合
完成第一版，但很难继续加入并行工具、会话恢复、后台任务或多前端。

用户侧表现：

- 相邻阶段容易出现重复、跳变或缺少明确归属。
- 很难回答“现在正在做什么、为什么在等、刚才哪一步失败了”。
- 新增一个显示规则时，常常需要同时修改 Renderer、TerminalApp 和测试夹具。

### P0：Renderer 在解析模型传输协议

审计基线中，`MODEL_DELTA` 携带模型生成的原始 JSON 片段，`TerminalRenderer`
内部实现 `_JsonStringFieldStreamer`，从不完整 JSON 中增量提取 `final_answer`，还
需要自己处理转义字符和 Unicode 代理对。当前阶段 C 切片已将该解析器迁移到
`PresentationAdapter`，Renderer 只消费 `AssistantTextDelta`。

这意味着终端展示层知道模型 wire format。未来接入 Anthropic tool use、OpenAI
Responses API 或其他原生工具协议时，Renderer 会被迫理解更多模型差异。

目标边界应为：Renderer 只接收“回答新增了这段文字”，不关心这段文字原来位于
JSON、SSE 还是 provider content block 中。

### P0：工具生命周期缺少稳定身份

现有事件通过工具名称和事件顺序关联：

```text
TOOL_REQUESTED → TOOL_STARTED → TOOL_COMPLETED
```

没有 `call_id`、`turn_id` 或单调 `sequence`。在目前“一轮一个工具、严格串行”的
前提下能够工作，但一旦出现并行只读工具、后台进程、重试或恢复，就无法可靠地
把结果更新到正确的界面块。

此外，`TOOL_STARTED` 在 `ToolExecutor.execute()` 之前发送，而权限确认发生在
`execute()` 内部，因此 UI 语义上会先进入“执行中”，随后才进入“等待授权”。
这两个状态应该被显式区分。

### P1：Transcript 保存的是渲染产物，不是语义内容

`TranscriptBlock` 深复制 Rich 对象，再按终端宽度缓存文本。这解决了窗口缩放，
但带来三个限制：

- `/verbose` 只影响后续输出，无法重新展开历史块。
- 无法稳定序列化、持久化或重放用户看到的会话。
- 其他 UI 必须重新解释终端文本，而不能复用同一份展示数据。

正确的长期模型应保存 `ToolBlock(title, status, preview, output_ref)`，Rich 对象只在
最后一层临时生成。

### P1：工具展示逻辑集中在一个 Renderer

审计基线中，工具名、参数摘要、结果摘要、Shell JSON 解码、编辑 diff 配色等逻辑都
集中在 `TerminalRenderer`。每增加一个内置工具、MCP 工具或插件工具，都需要扩展
中心化分支。当前基础切片已把请求与结果摘要迁移到 `ToolPresenterRegistry`；diff
样式等纯终端排版仍保留在 Renderer。

这会让 Renderer 同时负责：

- 状态机
- 工具领域知识
- 输出裁剪
- 文案
- Rich 排版

应增加独立的 `ToolPresenter` 注册表。内置工具可以有专属 Presenter，未知或 MCP
工具始终有通用 fallback。Presenter 返回纯数据，不能返回 Rich 对象，也不能执行
工具。

### P1：界面看起来像聊天，但任务实际上彼此独立

`TerminalApp` 会保存输入历史并让任务排队，但每次 `AgentRuntime.run()` 都创建新的
`AgentState`。因此屏幕看起来是一段连续对话，模型却不会继承上一轮上下文。

在真正支持会话上下文之前，界面必须诚实表达“排队的是独立任务”；支持会话后，
再把它升级成连续 Turn。不能只在视觉上伪装成聊天。

### P1：长会话缺少容量治理

当前 transcript 会持续保存 Rich 对象、ANSI 文本和格式化 fragments，没有容量限制
或虚拟化。现有文档也明确说明长会话尚未虚拟化。

短期应采用有界渲染窗口和语义块缓存，而不是立即实现复杂虚拟列表；会话持久化
完成后，离开窗口的旧块仍可从存储恢复。

### P2：视觉层级和文案不统一

- 品牌头、状态、工具记录、回答和 footer 都会争夺注意力。
- 中英文状态混用，例如 `Thinking`、`Responding`、`Done` 与中文命令文案并存。
- 成功结果、普通详情和真正需要用户处理的错误之间，对比度仍不够明确。
- 编辑完成后默认展示完整 diff，在大修改中容易淹没最终答案。
- 当前“工具紧凑/完整”是全局开关，不支持只展开某一个工具块。

新版默认采用克制的信息层级：用户任务和最终回答最突出，当前活动次之，成功工具
记录弱化，错误和权限请求保持高可见性。

### P2：脚本模式与交互模式存在双渲染路径

Renderer 既能直接控制 Rich `Live`，又能通过 callback 把内容交给 TerminalApp。
这增加了组合状态和测试矩阵。两种运行方式可以保留，但都应消费同一种语义
ViewModel；差异只发生在最后的输出适配层。

## 5. Claude Code 参考与 Mini-Agent 取舍

参考仓库是反编译/恢复版本，只用于理解模式，不把其规模和实现细节视为标准答案。

| Claude Code 中的模式 | 可借鉴部分 | Mini-Agent 的取舍 |
| --- | --- | --- |
| `FullscreenLayout.tsx` 将 scrollable、bottom、overlay、modal 分区 | 滚动历史和固定输入区是不同生命周期；权限选项应保持可见 | 保留现有单 Application 布局，明确四个区域，不引入 React |
| `Messages.tsx` 先 normalize、group、collapse，再渲染 | 原始消息不直接决定最终展示；探索类操作可以聚合 | 用 Presenter + Reducer 生成语义块，不复制庞大消息管线 |
| `MessageRow.tsx` 只让流式或未完成行持续更新 | 完成块应冻结，避免全历史重复排版 | `BlockStatus` 进入终态后只允许显式展开状态变化 |
| 工具提供用户可见名称、调用摘要和结果组件 | 工具展示应可扩展并有 fallback | 使用独立 `ToolPresenterRegistry`，避免 Tool Core 依赖 Rich |
| 不同工具路由到专属权限组件 | 文件 diff、Shell、网络访问需要不同预览 | 复用统一 Permission VM，按 presenter 提供 preview |
| 明确 queued、waiting permission、running、resolved、errored | 用户需要知道 Agent 是在等待还是执行 | 将这些状态纳入 ToolBlock，而不是依赖 spinner 文案 |
| 长会话使用消息上限、稳定 anchor 和虚拟滚动 | 长历史不能无限参与每次布局 | 第一阶段先做有界窗口，真实需求出现后再做虚拟化 |
| 离开底部后显示 unseen divider / jump-to-bottom | 新输出不能抢滚动位置，但也不能悄无声息 | 增加“有新内容 / 回到底部”，不只显示“正在查看历史” |

明确不引入：

- React、Ink、Zustand 或自研终端渲染器。
- Claude Code 的复杂消息类型、feature flag 和多层 Provider 树。
- 在没有规模数据前实现高度测量、overscan 和增量虚拟列表。
- 将 UI 组件方法直接加入 `Tool` 核心接口。

## 6. 新版交互原则

1. **真实**：清楚区分排队、思考、等待授权、执行、响应、失败和取消。
2. **稳定**：完成内容不因后续事件重写、闪烁或重复打印。
3. **渐进披露**：默认摘要，错误自动展开，详情由用户按块展开。
4. **单一焦点**：任何时刻只有一个主要 activity，历史保持低噪声。
5. **内容优先**：不让 Logo、边框和状态装饰抢占任务与答案空间。
6. **可回放**：相同事件序列必须得到相同语义界面状态。
7. **可移植**：ViewModel 不包含 Rich、ANSI、终端宽度或回调对象。
8. **键盘优先**：所有鼠标能力都有稳定的键盘路径。
9. **宽度安全**：40、80、120 列下均不丢失权限、错误和取消信息。

## 7. 状态模型

### 7.1 三层状态边界

`AgentState` 继续表示模型运行状态；`TurnViewModel` 表示用户可感知的一次任务；
`TerminalViewState` 只表示当前终端如何查看这些内容。三者不能合并。

```text
AgentState
    模型消息、Action、Observation、完整工具结果

SessionPresentation / TurnViewModel
    任务、阶段、语义内容块、权限请求、排队状态

TerminalViewState
    滚动、展开、选区、补全面板、终端宽度
```

这样既不会为了未来桌面端提前建设 IPC，也不会把终端特有状态固化进共享数据。

### 7.2 共享 Presentation State

以下对象只使用可转换为 JSON 的字段，不包含 Rich、ANSI、`Path`、异常、锁、回调或
终端宽度。

```python
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None


class TurnPhase(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    WAITING_PERMISSION = "waiting_permission"
    RUNNING_TOOLS = "running_tools"
    RESPONDING = "responding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BlockStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TextBlockViewModel:
    id: str
    role: str                 # user | assistant | commentary | system
    text: str
    streaming: bool = False


@dataclass(frozen=True)
class ToolBlockViewModel:
    id: str
    call_id: str
    tool_name: str
    kind: str                 # file_read | file_edit | shell | generic
    subject: str
    details: tuple[str, ...]
    status: BlockStatus
    summary: str | None = None
    metrics: Mapping[str, JsonScalar] | None = None
    preview: tuple[str, ...] = ()
    truncated: bool = False
    output_ref: str | None = None
    started_at_ms: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class NoticeBlockViewModel:
    id: str
    level: str                # info | warning | error
    title: str
    detail: str | None = None


ViewBlock: TypeAlias = (
    TextBlockViewModel | ToolBlockViewModel | NoticeBlockViewModel
)


@dataclass(frozen=True)
class TurnViewModel:
    id: str
    prompt: str
    phase: TurnPhase
    blocks: tuple[ViewBlock, ...]
    step: int
    started_at_ms: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class PermissionChoiceViewModel:
    id: str
    label: str
    description: str


@dataclass(frozen=True)
class PermissionRequestViewModel:
    request_id: str
    call_id: str
    kind: str                 # command | diff | path | network | generic
    title: str
    risk: str | None
    preview: tuple[str, ...]
    choices: tuple[PermissionChoiceViewModel, ...]


@dataclass(frozen=True)
class SessionPresentation:
    schema_version: int
    session_id: str
    turns: tuple[TurnViewModel, ...]
    active_turn_id: str | None
    queued_turn_ids: tuple[str, ...]
    permission_requests: tuple[PermissionRequestViewModel, ...]
    permission_mode: str      # ask | approve | full
    last_sequence: int
```

`summary` 是所有客户端都能使用的文本 fallback；`kind`、`subject` 和 `metrics` 允许
未来的高级客户端将路径做成链接、单独显示 diff 统计，而不必解析 summary 文案。

`output_ref` 是指向受控会话存储的 opaque identifier，不是任意文件路径。完整敏感
命令、模型上下文和未裁剪工具输出不能默认进入诊断快照。

这是目标形状，不要求第一批改动一次实现全部字段。首个切片只需支持单任务、文本块、
工具块和结束状态。

### 7.3 终端本地状态

以下状态只影响当前终端如何显示共享内容，不应该进入 `SessionPresentation`：

```python
@dataclass(frozen=True)
class TerminalViewState:
    follow_tail: bool = True
    anchor_block_id: str | None = None
    unseen_count: int = 0
    expanded_block_ids: frozenset[str] = frozenset()
    active_permission_id: str | None = None
    selected_permission_index: int = 0
    active_overlay: str | None = None
    command_palette_open: bool = False
    transcript_width: int = 80
```

因此：

- 工具是否展开属于 TerminalViewState，而不是 ToolBlock。
- 权限菜单选中了第几项属于 TerminalViewState，而不是 PermissionRequest。
- 滚动位置和未读数量属于 TerminalViewState，而不是 SessionPresentation。
- 共享状态保存 `permission_mode="ask"`，终端决定显示“需要确认”还是
  `Ask for approval`。

未来桌面端可以拥有自己的 `DesktopViewState`，终端和桌面端的展开、焦点与布局互不
影响。

`TurnViewModel` 不重复保存 `final_answer`：最终回答就是最后一个 assistant 文本块。
当前活动项也由 `streaming=True` 和 ToolBlock 的 `PENDING/RUNNING` 状态推导，不保存
单一 `active_block_id`，从而允许未来同时运行多个只读工具。队列顺序由
`queued_turn_ids` 表达，任务正文只保存在对应 Turn 的 `prompt` 中。

### 7.4 为什么不直接使用 AgentState

`AgentState.messages` 是给模型看的协议记录，包含 system prompt、JSON action 和完整
Observation；用户不应该看到这套格式。UI 还需要保存展开状态、滚动位置和审批焦点，
这些也不应该污染模型上下文。

因此必须保持：

```text
AgentState            = 模型和执行语义
SessionPresentation   = 跨客户端的展示语义
TurnViewModel         = 单次任务的展示语义
TerminalViewState     = 当前终端的本地交互语义
```

## 8. PresentationAdapter 与 ToolPresenter

### 8.1 PresentationAdapter

`PresentationAdapter` 负责把现有 Runtime 与权限生命周期事件规范化成共享 Reducer
可消费的事件。终端滚动、选择和展开动作不经过这里。第一阶段可以继续兼容当前
`AgentEvent`，避免先修改 Agent Loop。

它承担：

- 为一次任务分配稳定 `turn_id`；当前一轮任务只对应一次 run，不提前增加 attempt 层。
- 为工具请求分配稳定 `call_id`。
- 把原始 `MODEL_DELTA` 解码成 `AssistantTextDelta`。
- 将权限 callback 转成 `PermissionRequested` / `PermissionResolved`。
- 为没有 timestamp 的旧事件补充事件时间和已完成操作的 `duration_ms`。
- 将旧协议事件转换成强类型 payload。

`_JsonStringFieldStreamer` 第一阶段移动到这里；等模型适配层能够直接提供结构化文本
delta 后再删除。它不能继续留在 Renderer。

模型可能以单字符粒度产生 delta。Adapter 应在不改变文字顺序的前提下，将增量暂存
到 buffer，并在 16–33ms、遇到换行或收到阶段结束事件时 flush。这样终端仍然流畅，
但不会让每个字符触发一次完整状态归约和重绘。批处理参数属于实现细节，不进入共享
协议。

### 8.2 ToolPresenter

```python
class ToolPresenter(Protocol):
    def present_request(self, args: Mapping[str, Any]) -> ToolRequestView: ...
    def present_result(self, result: ToolResult) -> ToolResultView: ...
    def present_permission(self, args: Mapping[str, Any]) -> PermissionPreview: ...
```

Presenter 的结果同时提供语义字段和文本 fallback，例如：

```python
ToolResultView(
    kind="file_edit",
    subject="src/auth.py",
    metrics={"added_lines": 3, "removed_lines": 2},
    summary="Updated src/auth.py · +3 / -2",
    preview=("@@ -18,3 +18,3 @@", "- old", "+ new"),
    truncated=False,
    output_ref="session-output:call-7",
)
```

终端可以直接使用 `summary`，未来支持链接和侧栏的客户端可以使用 `subject` 与
`metrics`，不需要反向解析文案。

建议首批 Presenter：

- `FileReadPresenter`
- `FileWritePresenter`
- `FileEditPresenter`
- `SearchPresenter`
- `ShellPresenter`
- `WebPresenter`
- `GenericToolPresenter`

Generic Presenter 必须保证未知工具仍可显示；这对未来 MCP 很重要。Presenter 只负责
标题、摘要、预览和风险说明，不负责 Rich 样式、权限决策或工具执行。

## 9. Reducer 设计

共享 Presentation 和终端本地状态分别只有一个写入口：

```python
def reduce_presentation(
    state: SessionPresentation,
    event: PresentationEvent,
) -> SessionPresentation:
    ...


def reduce_terminal(
    state: TerminalViewState,
    action: UiAction,
) -> TerminalViewState:
    ...
```

`reduce_presentation()` 处理 Runtime、权限和任务队列的客观变化；
`reduce_terminal()` 处理滚动、选择、展开和 overlay 等当前客户端行为。两个 Reducer
都必须是确定性的纯逻辑：

- 不读取终端宽度。
- 不调用 Rich 或 `prompt_toolkit`。
- 不执行文件、网络或 Shell 操作。
- 不直接读取当前时间；事件提供 `occurred_at_ms` 和已完成操作的 `duration_ms`。
- 不通过工具名称猜测对应关系，统一使用 ID。
- 对重复的终态事件保持幂等，不能重复添加块。
- 对未知事件安全忽略或生成诊断 Notice，不能破坏已有 transcript。

### 9.1 命令与本地视图动作

终端输入不能全部作为 UiAction 处理。Controller 需要先区分：

```text
本地视图动作
  Scroll / FollowTail / ToggleBlock / MoveSelection / OpenPalette
      → reduce_terminal()

Agent 命令
  SubmitTurn / CancelTurn / RespondPermission / ChangePermissionMode
      → SessionController / Runtime
      → 产生新的 PresentationEvent
      → reduce_presentation()
```

Reducer 永远不直接启动任务、取消进程或提交权限结果。这样未来其他客户端只是新增
命令入口，不会获得绕过 Python Core 权限判断的能力。当前不需要实现通用 command
bus；`TerminalApp` 可以先承担轻量 SessionController 职责，但方法边界要按上述两类
命名和测试。

### 9.2 事件归约表

| 事件 | Turn 变化 | Block 变化 |
| --- | --- | --- |
| `TaskQueued` | 新建 `QUEUED` Turn，加入 queued_turn_ids | prompt 保存在 Turn 上 |
| `RUN_STARTED` | 从队列移除、进入 `THINKING`，记录开始时间 | prompt 不重复写入块列表 |
| `MODEL_STARTED` | `THINKING`，更新 step | 设置当前 activity |
| `AssistantCommentary` | phase 不变 | 追加或合并 commentary 块 |
| `AssistantTextDelta` | `RESPONDING` | 按 block_id 追加文本 |
| `TOOL_REQUESTED` | phase 暂不改变 | 新建 `PENDING` ToolBlock |
| `PermissionRequested` | `WAITING_PERMISSION` | ToolBlock 保持 pending，加入 permission_requests |
| `PermissionResolved(allow)` | `RUNNING_TOOLS` | ToolBlock 进入 running，移除对应 request |
| `PermissionResolved(deny)` | 按剩余活动块推导 | ToolBlock 进入 failed，移除对应 request |
| `TOOL_STARTED` | `RUNNING_TOOLS` | 对应 ToolBlock 进入 running |
| `TOOL_COMPLETED` | 按剩余活动块推导 | 更新对应 ToolBlock 的结果和终态 |
| `RUN_COMPLETED` | `COMPLETED` | 完成回答块并清除 activity |
| `RUN_FAILED` | `FAILED` | 添加错误 Notice，清除待处理权限 |
| `RUN_CANCELLED` | `CANCELLED` | 关闭活动块，添加取消 Notice |

### 9.3 不变量

1. 一个 `call_id` 最多对应一个 ToolBlock。
2. 一个 Turn 可以有多个 pending/running ToolBlock，但最多有一个 streaming assistant
   文本块。
3. Turn phase 必须能从块状态和待处理权限推导，不能与它们矛盾。
4. `COMPLETED`、`FAILED`、`CANCELLED` 是 Turn 终态。
5. ToolBlock 进入终态后，Runtime 事件不能重新改成 running。
6. 权限请求必须引用存在且尚未完成的 `call_id`；多个请求按 tuple 顺序等待处理。
7. `follow_tail=False` 时，新块只让 TerminalViewState 增加 `unseen_count`，不能移动
   viewport anchor。
8. 展开、选择和滚动动作只能修改 TerminalViewState，不能修改共享工具执行数据。
9. 相同初始状态和事件序列产生结构相同的 SessionPresentation。
10. 相同 SessionPresentation 可以被不同客户端以不同展开和布局状态同时查看。
11. 事件中的 sequence 在同一 session 内严格递增；重复 sequence 必须幂等忽略。

实现时不要求每个流式字符都深复制完整对象树。可以由单线程 Store 在内部高效累积，
但只有 Reducer 可以修改语义状态，对外发布的 Snapshot 必须不可变、可序列化。流式
delta 按帧合并后再进入 Reducer。

## 10. Renderer 边界

新版 Renderer 只完成：

```text
SessionPresentation + TerminalViewState + theme
                         ↓
                 prompt_toolkit fragments
```

Renderer 不再：

- 解析 JSON。
- 持有工具生命周期状态。
- 根据事件顺序猜测当前工具。
- 决定权限是否允许。
- 保存未裁剪的原始工具输出。
- 直接修改任务队列或 Runtime 模式。

Rich 可以继续负责 Markdown、Syntax、Table 和 diff 的最终排版，但 Rich renderable 不应
写入 ViewModel。终端缩放时用同一语义块重新渲染即可。

交互式和单任务模式共用 Presenter、共享 Reducer 和块 Renderer：

- 交互模式使用 `SessionPresentation + TerminalViewState`。
- 单任务模式按事件更新同一个 SessionPresentation，将完成块顺序写入 stdout。
- `TerminalController` 负责把按键转换为 UiAction 或 Runtime command，不直接改
  Presentation 数据。

## 11. 默认视觉与信息层级

### 11.1 普通执行

```text
› 修复登录模块的 token 过期判断

  ● Read src/auth.py
  ● Search "expires_at" in src
  ● Edit src/auth.py
    └ Updated · +3 / -2
  ● Bash pytest tests/test_auth.py
    └ Passed · 12 tests · 1.8s

✦ 已修复过期时间比较逻辑，并补充了边界测试。

  ✓ 4 steps · 3.2s

────────────────────────────────────────────────────────────
› 输入下一条任务
  kimi-k2.5 · ~/project                         ask for approval
```

规则：

- 启动页只在空会话显示一次；首个任务后不保留大块品牌面板。
- 成功的读取、搜索和列表操作使用弱化单行，可连续聚合。
- 编辑和 Shell 显示结果摘要；完整 diff/输出按块展开。
- 失败块使用图标和文字，不只依靠颜色。
- 最终回答是当前 Turn 最突出的内容。
- 完成统计弱化，不在每一步重复计时。

### 11.2 等待权限

操作预览属于 transcript 中的 ToolBlock，选择器固定在输入区上方：

```text
  ● Bash
    $ python -m pytest

────────────────────────────────────────────────────────────
允许执行这条命令吗？
❯ 1. 允许一次
  2. 本次会话允许匹配命令
  3. 拒绝
  Enter 确认 · Esc 拒绝
────────────────────────────────────────────────────────────
```

批准范围必须使用具体能力描述，不能只写“始终允许 shell”。细粒度权限完成前，界面
要明确说明当前选择实际会允许本会话所有同名工具调用。

### 11.3 离开底部

```text
             2 条新内容 · End 回到底部 ↓
────────────────────────────────────────────────────────────
› 输入仍然可用
```

新内容不改变滚动锚点；回到底部后清零 unseen count。

### 11.4 窄终端

40 列左右时按以下顺序降级：

1. 隐藏耗时和成功详情。
2. 截断 workspace，保留权限模式。
3. 工具参数只保留标题。
4. 权限选项保持完整可读，必要时增加高度。
5. 错误原因和取消状态不得被省略。

## 12. 交互契约

| 输入 | 普通状态 | 有选区 | 任务运行中 | 权限窗口 |
| --- | --- | --- | --- | --- |
| `Enter` | 提交输入 | 提交输入 | 排队并显示位置 | 确认当前选项 |
| `Alt+Enter` / `Ctrl+J` | 换行 | 换行 | 换行 | 不适用 |
| `Ctrl+C` | 清空输入 | 复制选区 | 取消当前任务 | 拒绝并取消当前请求 |
| `Esc` | 关闭补全 | 清除选区 | 不取消任务 | 拒绝/关闭 overlay |
| `Ctrl+O` | 展开当前块 | 同左 | 同左 | 展开权限预览 |
| `PageUp/Down` | 浏览历史 | 浏览历史 | 浏览历史 | 浏览预览和历史 |
| `Ctrl+End` | 回到底部 | 回到底部 | 回到底部 | 回到当前请求 |

鼠标点击和滚轮是增强能力，不能成为完成任务所必需的唯一入口。

## 13. 与 Runtime 事件契约的演进

第一阶段不修改模型必须返回 `action` 或 `final_answer` 的协议。建议逐步给
`AgentEvent` 增加以下元数据，并提供默认值保持现有测试和调用方兼容：

```python
@dataclass(frozen=True)
class AgentEvent:
    type: EventType
    data: Mapping[str, Any]
    turn_id: str | None = None
    sequence: int | None = None
    occurred_at_ms: int | None = None
```

工具事件 payload 增加 `call_id`。长期应让 `TOOL_STARTED` 真正表示工具已通过权限
检查并开始执行；权限等待使用单独事件。不要为了 UI 方便改变 `ToolResult` 的恢复
语义。

内部测量耗时可以继续使用 `time.monotonic()`，但 monotonic 值不能跨进程解释。事件
对外只提供 Unix 时间 `occurred_at_ms` 和已计算好的 `duration_ms`。事件顺序以
`sequence` 为准，不能依靠不同时钟的时间戳排序。

活动中的动态计时属于客户端动画状态：TerminalController 在收到开始事件时，以
`turn_id` 或 `call_id` 为键记录本地 monotonic 起点；操作完成后改用 Core 给出的
`duration_ms`。这些本地计时值不序列化，也不参与事件重放结果比较。

模型层最终应提供语义化流事件：

```text
assistant_status_delta
assistant_text_delta
tool_input_delta
```

在此之前，由 `PresentationAdapter` 兼容当前原始 JSON delta。

### 13.1 序列化边界

现在不实现桌面端或 IPC，但从第一版开始要求 `SessionPresentation` 能通过
`to_dict()` / `from_dict()` 完整往返 JSON。序列化结果包含 `schema_version`，枚举
输出稳定字符串值。路径、异常和 ToolResult 等 Python 对象必须先投影为普通数据。

未来真正建立进程边界时，再把内部事件包装成 transport envelope：

```json
{
  "schema_version": 1,
  "event_id": "event-42",
  "session_id": "session-1",
  "turn_id": "turn-3",
  "sequence": 42,
  "type": "tool.completed",
  "occurred_at_ms": 1791590000000,
  "payload": {
    "call_id": "call-7",
    "success": true,
    "duration_ms": 1840
  }
}
```

这只是协议保留，不在当前 TUI 重构中实现 JSON-RPC、WebSocket、Daemon、鉴权或
TypeScript 类型生成。

## 14. 分阶段迁移方案

### 阶段 A：锁定行为基线

不改变生产行为：

1. 建立包含“工具成功、工具失败、协议重试、权限允许/拒绝、取消、最终回答”的
   事件 trace fixture。
2. 为 40、80、120 列输出建立渲染快照；ANSI 样式单独测试，不把所有颜色码写进
   大型 golden file。
3. 记录当前人工验收脚本和终端环境。

验收：现有 UI 测试不变，fixture 可以确定性重放。

### 阶段 B：引入 ViewModel 和 Reducer

建议新增：

```text
ui/view_model.py      # 共享数据、TerminalViewState、两个 reducer
ui/presentation.py    # AgentEvent 适配和 ToolPresenterRegistry
tests/test_view_model.py
```

第一条垂直链路只覆盖：

```text
RUN_STARTED
→ MODEL_STARTED
→ TOOL_REQUESTED/STARTED/COMPLETED
→ MODEL_STARTED
→ AssistantTextDelta
→ RUN_COMPLETED
```

此时旧 Renderer 仍可工作，新旧路径用同一事件 fixture 对比信息完整性。
同时增加 `SessionPresentation` 的 JSON 往返测试，但不增加网络服务。

### 阶段 C：Renderer 改为消费 ViewModel

当前进度：本阶段已完成。Renderer 复用 Presenter 并通过语义化事件渲染；稳定
TranscriptBlock 覆盖 prompt、回答、commentary 和工具结果；`Ctrl+O` 与 `/verbose`
可以重新投影历史块。斜杠命令结果等本地通知继续使用无 ID 的兼容块。

1. 将工具摘要和结果格式搬入 Presenter。
2. 将 JSON delta 解码搬出 Renderer。
3. TerminalApp 只保存 TerminalViewState；语义块由 SessionPresentation 持有。
4. 删除 `_tool_request_printed`、`_has_answer_stream` 等可以由 ViewModel 推导的状态。
5. `/verbose` 改成块级展开，并允许重新渲染历史。
6. 将模型文字 delta 合并到最多约 30 FPS，并在换行、工具请求和完成事件前 flush。

验收：同一个 trace 多次重放不重复内容；切换宽度和展开状态不改变语义数据。

### 阶段 D：权限、队列和取消统一入模

当前进度：已完成本地 TUI 范围。排队任务复用稳定 turn ID；交互式权限请求使用
request ID + call ID；允许、拒绝、取消和关闭都会收敛共享状态与本地 overlay。

1. 权限请求获得 `request_id` 和 `call_id`。
2. 区分 pending、waiting permission 和 running。
3. 排队任务显示在 SessionPresentation；开始执行时从 queue 原子迁移到 active turn。
4. 取消和关闭清理所有关联 overlay，不能遗留等待线程。

验收：权限与取消的所有排列都有状态转换测试，worker 异常后下一任务仍可执行。

### 阶段 E：视觉精修和长会话

当前进度：本地 TUI 项目已完成。保留品牌 Logo 启动卡片；支持最近块 `Ctrl+O`、历史
未读提示、回到底部和 Transcript 块预算。会话持久化与真实虚拟化仍遵循下列前置
条件，不在本轮凭空引入。

1. 使用本文默认信息层级重写块 Renderer。
2. 增加按块展开、未读计数和回到底部。
3. 增加 transcript 块数/行数预算。
4. 会话持久化完成后，让旧块可按需载入。
5. 只有性能数据证明有必要时，再实现真实虚拟化。

### 阶段 F：决定是否增加 TypeScript 客户端

完成上述层次分离后，再用同一组 JSON fixture 做一个独立的 TypeScript/Ink 原型。
只有当它在以下方面显著胜出时才保留：

- 终端重绘稳定性。
- 组件开发和维护成本。
- 长会话性能。
- VS Code/Web 复用价值。
- 打包和跨平台体验。

即使选择 TypeScript，也只替换客户端，不重写 Python Agent Core。

当前阶段明确延后：

- JSON-RPC、WebSocket 和后台 Daemon。
- Electron、Tauri、React 或 Ink 工程。
- 从 Python schema 自动生成 TypeScript 类型。
- 多客户端状态同步和远程鉴权。
- 没有性能数据支撑的完整虚拟列表。

## 15. 测试策略

### 共享 Reducer 单元测试

- 每一种事件的状态转换。
- 重复终态事件的幂等性。
- 无效或乱序事件的安全降级。
- 两个工具调用不能串结果。
- 权限请求必须关联正确 call。
- 取消时活动块和权限请求同时收敛。
- 重复或倒退的 sequence 不会重复修改状态。
- `SessionPresentation` 可以无损 JSON 往返。

### Terminal Reducer 单元测试

- follow-tail、anchor 与 unseen count。
- 展开和折叠不改变共享工具执行数据。
- 权限选项移动不改变 PermissionRequest。
- 关闭 overlay 后焦点回到输入框。
- 相同 SessionPresentation 可以对应多个互不影响的 TerminalViewState。

### Presenter 单元测试

- 每个内置工具的请求摘要、成功摘要和失败摘要。
- 未知工具 fallback。
- 路径、命令、URL 和 diff 的敏感内容裁剪。
- Unicode、控制字符、超长单行和无换行输出。
- 原始 JSON delta 的任意 chunk 边界。
- 16–33ms 批处理、换行 flush 和阶段切换前 flush 不丢失或重排文本。
- `kind`、`subject`、`metrics` 与 summary fallback 保持一致。

### Renderer 测试

- 40、80、120 列布局。
- 中英文、emoji、Markdown 表格和代码块。
- 同一组 SessionPresentation 和 TerminalViewState 重复渲染结果一致。
- 完成块不会因 activity tick 改变。
- 历史块可以在 `/verbose` 或块级展开后重新渲染。
- 颜色关闭时仍可从图标和文字判断状态。

### 端到端交互测试

- 执行中继续输入并排队。
- 浏览历史时收到新内容。
- 权限期间查看长 diff、允许、拒绝和取消。
- Shell 子进程运行时取消。
- resize、复制选区、补全 overlay 同时出现时的优先级。
- 单任务模式与交互模式展示相同的语义结果。

### 人工体验验收

每次 UI 里程碑至少在 macOS Terminal、iTerm2 和一个 40 列窄窗口完成：

1. 小型问答。
2. 多文件读取与搜索。
3. 编辑文件并审批 diff。
4. 运行有持续输出的测试。
5. 工具失败后恢复。
6. 流式中文、emoji 和代码块回答。
7. 任务中滚动历史并返回底部。
8. 取消后立即提交下一任务。

## 16. 验收指标

新版交互层完成时应满足：

- Renderer 中不存在模型 JSON 字段解析逻辑。
- Renderer 不直接消费 `Mapping[str, Any]` 类型的 Runtime payload。
- 所有工具块通过 `call_id` 更新，不依赖“最后一个工具”或工具名称匹配。
- 同一事件 trace 可以序列化、重放并得到一致的 SessionPresentation。
- SessionPresentation 与 TerminalViewState 没有字段职责重叠。
- 共享状态可以 JSON 往返，不包含 Rich、ANSI、`Path`、异常、锁或 callback。
- 切换块展开状态只改变 TerminalViewState。
- 连续流式小片段按帧合并，阶段切换前不会遗失尾部文本。
- 任务完成后没有 live spinner、权限等待或可变输出块残留。
- 浏览历史时新内容不抢位置，并明确显示未读数量。
- 默认界面不展示完整读取结果和完整 Shell 输出。
- 错误、拒绝和取消不依赖颜色也能辨认。
- 终端缩放不会重复内容或丢失当前活动状态。
- 现有取消、权限、队列、选择和跨平台测试继续通过。

## 17. 最小首个实现切片

第一批代码不应同时重写整个 TUI。推荐只做：

1. 新增 `ui/view_model.py`，定义 Turn、SessionPresentation、TerminalViewState 和
   两个 Reducer 的最小子集。
2. 新增 `ui/presentation.py`，把当前 `AgentEvent` 转换为强类型展示事件，并合并
   高频文字 delta。
3. 为 action → observation → final answer 建立一个可重放 fixture。
4. 增加共享状态 JSON 往返测试，暂不实现进程间通信。
5. 让 `TerminalRenderer` 从 SessionPresentation + TerminalViewState 渲染这一条
   链路，权限和滚动暂时沿用旧实现。
6. 新旧实现输出语义一致后，再删除对应旧状态字段。

这个切片足以验证设计价值，又不会同时触碰任务队列、权限线程和滚动锚点三个高风险
区域。

## 18. 最终决策

- **现在不进行 TypeScript 重写。**
- **先建立共享 SessionPresentation / TurnViewModel 与独立 TerminalViewState。**
- **继续使用现有 Runtime 协议和 Python 工具层。**
- **通过 Presenter 隔离模型协议和工具展示。**
- **共享 Reducer 只处理客观事件，Terminal Reducer 只处理本地交互。**
- **以可序列化快照、可回放事件、块级展开和稳定滚动作为新版 UI 的基础。**
- **未来如需 VS Code/Web，再在稳定协议之上增加 TypeScript 客户端。**

这条路线既保留 Mini-Agent 的简洁性，也为更成熟的终端体验、会话持久化、MCP、
后台任务和多前端留下了清晰扩展点。
