# pi-py

A layered Python port of the [pi agent harness](https://github.com/earendil-works/pi). Three
packages, one direction of dependency:

| Layer | Package | Role | Depends on |
| --- | --- | --- | --- |
| Atom | `pi-ai` | Unified LLM API: types, streaming events, providers, credentials, cost | *nothing* |
| Molecule | `pi-agent` | Agent runtime: loop, tools, hooks, events, queues | `pi-ai` |
| Compound | `pi-coding-agent` | Coding tools, session tree, compaction, skills, extensions, CLI | `pi-agent` |

Each package installs and tests standalone. Nothing ever imports upward — see
[ARCHITECTURE.md](ARCHITECTURE.md) for how that is enforced.

## Install

```bash
uv sync                      # workspace install, all packages editable
# or, per layer:
pip install -e packages/pi-ai
pip install -e packages/pi-agent
pip install -e packages/pi-coding-agent
pip install -e packages/pi-tui
pip install -e packages/pi-app
pip install -e packages/pi-server
```

## Configure Models

### mac
~/.pi/agent/models.json:
```json
{
  "providers": [
    {
      "id": "pi-planing",
      "name": "PI Planning",
      "baseUrl": "****",
      "api": "anthropic-messages"
    }
  ],
  "models": [
    {
      "id": "glm-5.2",
      "provider": "pi-planing",
      "api": "anthropic-messages",
      "name": "glm-5.2",
      "contextWindow": 200000,
      "maxTokens": 16384,
      "reasoning": true,
      "baseUrl": "***"
    },
    {
      "id": "deepseek-v4-pro",
      "provider": "pi-planing",
      "api": "anthropic-messages",
      "name": "deepseek-v4-pro",
      "contextWindow": 128000,
      "maxTokens": 8192,
      "baseUrl": "***"
    }
  ]
}
```
~/.pi/agent/auth.json:
```json
{
  "pi-planing": { "apiKey": "***" }
}
```

Python 3.10+. The only runtime dependency in the whole tree is `httpx`, and only for
actually talking to a provider — `pi-ai` imports it lazily, so the layer is fully
importable and testable without it.

## Run
TUI
```bash
pi-tui
```
web
```bash
# Terminal 1: Backend pi‑web‑server
pi-web            # 或 python -m pi_server.app
# Default: http://127.0.0.1:8848

# Terminal 2: Frontend
cd packages/pi-web
npm install
npm run dev              # http://localhost:5173
```

## Scope

Ported: the streaming/provider abstraction, the agent loop with parallel tool execution
and hooks, steering and follow-up queues, the seven built-in coding tools, the JSONL
session tree with branching and compaction, skills, prompt templates, extensions,
settings, and a CLI.

Not ported: OAuth subscription login, the generated model catalog,
themes, HTML export, RPC mode, telemetry, and the package-distribution system.

## Differences from upstream pi

- `grep` / `find` use `ripgrep` / `fd` when present and fall back to pure Python
  otherwise; upstream requires the binaries.
- `bash` defaults to a 120 s timeout, then kills the whole process group.
- Tool parameters are plain JSON Schema dicts instead of TypeBox, validated by a small
  built-in checker that also coerces the common `"3"` → `3` mistake.
- The session tree keeps pi's on-disk format (camelCase keys, v3 entry types), so
  transcripts stay readable by the TypeScript implementation.

## Acknowledgements
This project draws inspiration and references from two open‑source projects under the **TIM protocol**:

- **pi‑agent**: Official repository for pi‑agent, providing reference implementations for agent runtime, tool‑calling logic and session state management.
  GitHub: <https://github.com/earendil‑works/pi>

- **pi‑web**: Browser‑based web UI for Pi Agent, offering reference designs for session workspace, streaming interaction and client‑server communication.
  GitHub: <https://github.com/agegr/pi‑web>

> Statement: This project **does not directly copy source code from upstream repositories, and only learns from their architecture, protocols and design ideas**. If any upstream source code is copied or reused in the future, the corresponding open‑source licenses of upstream repositories shall be strictly complied with.

Special thanks to all contributors of these repositories for publishing their code and documentation publicly.

MIT, like upstream.
