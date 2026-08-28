# pi-agent 测试排错对照表

某条断言红了，直接查这里，不用从头读代码。

## 参数校验 → `validation.py`

| 断言 | 看什么 |
|---|---|
| 填充默认值 | `validate_tool_arguments` 开头那段：遍历 `properties`，`"default" in sub` 时补进 args |
| 字符串数字/布尔被纠正 | `_check` 里 `expected in ("number", "integer") and isinstance(value, str)` 的容错分支 |
| 拒绝缺必填 | `required` 循环有没有把 key 缺失记进 `errors` |
| 拒绝类型不符 | `_TYPES` 映射；注意 `bool` 是 `int` 的子类，必须先单独判掉 |
| 递归检查数组元素 | `expected == "array"` 分支里对 `items` 的递归 |

## 消息转换 → `loop.py::default_convert_to_llm`

| 断言 | 看什么 |
|---|---|
| 过滤纯 UI 消息 | `if m.include_in_context` 这个判断在不在 |
| 自定义消息折成 user | `CustomMessage` 分支要构造 `UserMessage`，不是原样 append |
| 顺序不变 | 别用 dict 去重或排序，就是顺序遍历 |

## 队列 → `queue.py`

| 断言 | 看什么 |
|---|---|
| 空的取出空列表 | `take()` 必须 `return []`，**不能 return None** |
| 逐条模式一次一条 | `mode == "one-at-a-time"` 时 `self.items.pop(0)` |
| 取完就空 | 全取模式要 `taken, self.items = self.items, []`，不是只返回不清空 |

## 循环主流程 → `loop.py::run_agent_loop`

| 断言 | 看什么 |
|---|---|
| 生命周期事件齐全 | `AgentStartEvent` 在最前、`AgentEndEvent` 在 `finally` 之后 |
| transcript | 每轮 `state.messages.append(assistant)` **和** `new_messages.append(assistant)` 两处都要有 |
| 用量跨轮累加 | 这是 `AgentState.usage()` 的事，检查它遍历了所有 assistant 消息 |
| 结束后不在流式中 | `finally` 里的 `state.is_streaming = False` |
| 两个回合 | 有工具调用时 `continue` 而不是 `break` |
| 连续: 第二轮真的跑了 | ⚠️ `Agent.prompt` 的 `finally` 里必须是 `self._idle.set()`，写成 `_cancel.set()` 会让下一轮立刻 aborted |
| wait_for_idle 不会卡住 | 同上 |
| 并发保护 | `prompt()` 开头 `if self.state.is_streaming: raise` |

## 工具执行 → `loop.py::execute_tool_calls`

| 断言 | 看什么 |
|---|---|
| 未知工具不崩 | ⚠️ `if tool is None:` 分支**结尾必须有 `continue`**，否则下一行拿 `None.parameters` |
| 参数非法转成错误结果 | `except ValidationError` 要产出 `ToolResultMessage` 并 `continue`，不能让异常冒泡 |
| 工具抛异常循环不中断 | `_run_one_tool` 里 `except Exception` 兜住，转成 `is_error=True` 的结果 |
| **并行: 结束事件按完成顺序** | 每个 task 在 `_run_one_tool` 内部自己 emit end；不要收集完再统一 emit |
| **并行: 结果消息按请求顺序** | 结果写进 `results[index]`，最后按下标取；**不要用 `gather` 的返回值** |
| 并行: 开始事件按请求顺序 | `tool_execution_start` 在准备阶段的顺序循环里发，不在 task 里发 |
| 串行: 不重叠执行 | `tool_execution == "sequential"` 时 `for ... await run(...)`，不是 `gather` |
| 流式工具收到中间更新 | `ToolContext.on_update` 有没有接到 emit；`ctx.update()` 里判断 `if self.on_update` |

## 钩子 → `loop.py`

| 断言 | 看什么 |
|---|---|
| 前置钩子真的拦住了 | `if hook and hook.block:` 分支要 `continue`，且不能把 call 加进 `prepared` |
| 前置钩子可改写参数 | `hook.arguments is not None` 时覆盖 `arguments`，注意要在 block 判断之前 |
| 后置钩子可替换结果 | `_run_one_tool` 里 `result = hook.result or result` |
| 后置钩子 terminate | `terminate` 要一路传回 `execute_tool_calls` 的返回值，再传到主循环 |
| 上下文转换被调用 | `_stream_turn` 开头 `if config.transform_context` |
| 上下文转换不影响真实 transcript | 转换结果只喂给 `Context`，**不要写回 `state.messages`** |

## 队列与中断

| 断言 | 看什么 |
|---|---|
| 引导被注入 transcript | `config.on_turn_end` 在工具结果之后调用，返回值 extend 进两个列表 |
| 引导插在工具结果之后 | 调用位置在 `emit(TurnEndEvent(...))` 之后、下一轮之前 |
| 引导消息事件成对 | 注入消息要同时 emit `MessageStartEvent` 和 `MessageEndEvent` |
| 后续: 模型停下时才注入 | `config.on_before_stop` 在 `if not calls:` 分支里 |
| 中断: 工具能感知 | `ToolContext.cancel_event` 传的是同一个 Event 对象，不是拷贝 |
| 中断: 结束原因 aborted | 工具批次跑完后有 `if cancel_event.is_set(): reason = "aborted"; break` |
| 中断: 状态复位 | `finally` 里 `pending_tool_calls.clear()` |
| 回合上限 | `if config.max_turns is not None and turn >= config.max_turns` 在 `turn += 1` **之前** |

## 错误路径

| 断言 | 看什么 |
|---|---|
| 流出错循环优雅退出 | `assistant.stop_reason in ("error", "aborted")` 分支 |
| 无终止事件有兜底消息 | ⚠️ `_stream_turn` 结尾 `if final is None:` 那段，删了会返回 None 一路崩 |
| 续跑 assistant 结尾应拒绝 | `agent_loop_continue` 里 `isinstance(last, AssistantMessage)` 时 raise |
| 续跑空 transcript 应拒绝 | 同函数开头 `if not state.messages` |

## 订阅

| 断言 | 看什么 |
|---|---|
| 取消后不再收到 | `subscribe` 返回的闭包要从 `self._listeners` 里 remove |
| 监听器异常不影响主流程 | `_emit` 里 `try/except` 包住每个 listener 的调用 |

---

## 这份测试的可信度

我做过变异测试——故意改坏实现，看测试是否变红：

| 故意引入的 bug | 结果 |
|---|---|
| 删掉未知工具分支的 `continue` | ✅ 抓到 |
| `finally` 里写成 `self._cancel.set()` | ✅ 抓到（wait_for_idle 超时） |
| 忽略 `include_in_context` 过滤 | ✅ 抓到 |
| 删掉 `_stream_turn` 的兜底返回 | ✅ 抓到 |
| 忽略前置钩子的 block | ✅ 抓到 |
| 串行模式失效（退化成并行） | ✅ 抓到 |
| 无害改动（对照组） | ✅ 正确地不报警 |

七个变异全部按预期反应，说明这份测试确实在检验行为而不是走过场。
