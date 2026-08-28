# pi-agent

The molecular layer: the loop. Call the model, run the tools it asked for, feed the
results back, repeat. Depends only on `pi-ai`.

```python
import asyncio
from pi_agent import Agent, AgentState, AgentTool, AgentToolResult

async def add(args, ctx):
    return AgentToolResult.text(str(args["a"] + args["b"]))

tool = AgentTool(
    name="add",
    description="Add two numbers",
    parameters={"type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"]},
    execute=add,
)

agent = Agent(stream_fn=models.stream_simple,                 # the inversion point
              initial_state=AgentState(model=model, tools=[tool]))
agent.subscribe(lambda e: print(e.type))
asyncio.run(agent.prompt("what is 17 plus 25?"))
```

Key pieces: `run_agent_loop` (turn state machine), `execute_tool_calls` (validation →
`before_tool_call` → parallel or sequential execution → `after_tool_call`, with
`tool_execution_end` in completion order and results in source order), `Agent` (state,
events, steering / follow-up queues, abort), `CustomMessage` (application-defined
messages that the loop stores and optionally shows the model), and
`validate_tool_arguments`.

This layer never touches the disk or spawns processes — the import-linter contract in
the repo root enforces it.
