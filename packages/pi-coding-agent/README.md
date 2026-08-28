# pi-coding-agent

The compound layer: a coding agent. Depends on `pi-agent` (and transitively `pi-ai`).

- **Tools** — `read` `write` `edit` `bash` `grep` `find` `ls`, with actionable truncation.
  `edit` falls back to a fuzzy match (trailing whitespace, smart quotes, unicode dashes)
  and returns a unified patch.
- **Sessions** — JSONL entry tree with `id`/`parent_id`, in-place branching, labels,
  model/thinking-level changes, and compaction checkpoints that carry their retained tail.
- **Compaction** — summarize everything but the last few turns when the context window
  fills; wired in through the agent layer's `transform_context` hook.
- **Resources** — skills (`SKILL.md`), prompt templates (`.md` slash commands),
  extensions (`activate(pi)` modules), `AGENTS.md` context files.
- **Runtime** — `ModelRuntime` (auth + custom models), `SettingsManager` (global settings
  merged with project settings), `AgentSession` (the SDK surface), and a CLI with
  interactive / print / JSON modes.

```python
from pi_coding_agent import create_agent_session

session = create_agent_session(cwd=".", tools=["read", "edit", "bash"])
await session.prompt("make the failing test pass")
```

See the repository README for the CLI, and ARCHITECTURE.md for the layering rules.
