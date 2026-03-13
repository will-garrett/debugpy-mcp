# debugpy-mcp

An enhanced MCP server for Cursor that helps an agent attach `debugpy` to an already-running Python process inside a Docker container, inspect context, retrieve logs, and suggest breakpoint plans for FastAPI-style services.

## Features

- List running containers
- Autodiscover a likely target container by name, image, or service hint
- Inspect whether a target container is ready for `debugpy` attach
- Inject `debugpy` into a live PID inside the container
- Prefer uvicorn and gunicorn worker processes automatically
- Return rich debugging context for an agent
- Fetch recent container logs and debugpy log files
- Suggest a breakpoint plan from logs and process metadata

## Tools

- `debugpy_connect` — check if debugpy is already listening at a host:port (no Docker required)
- `debugpy_list_containers`
- `debugpy_autodiscover_target`
- `debugpy_status`
- `debugpy_attach`
- `debugpy_context`
- `debugpy_logs`
- `debugpy_debugpy_logs`
- `debugpy_breakpoint_plan`

## Install

### pip / venv

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### uv

```bash
uv venv
uv pip install -e .
```

Or install directly without cloning:

```bash
uv tool install debugpy-mcp
```

### Poetry

```bash
poetry install
```

## Run manually

### pip / venv

```bash
debugpy-mcp
```

### uv

```bash
uv run debugpy-mcp
```

### Poetry

```bash
poetry run debugpy-mcp
```

## Cursor MCP config

Cursor reads MCP server config from `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project-local).

### pip / venv

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "/absolute/path/to/debugpy-mcp/.venv/bin/debugpy-mcp",
      "args": []
    }
  }
}
```

### uv (recommended)

If you installed with `uv tool install`, `uvx` can run the server without activating an environment:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "uvx",
      "args": ["debugpy-mcp"]
    }
  }
}
```

If you installed with `uv pip install -e .` into a project venv, point directly at the binary:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "/absolute/path/to/debugpy-mcp/.venv/bin/debugpy-mcp",
      "args": []
    }
  }
}
```

Or run via `uv run` from the project directory:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/debugpy-mcp", "run", "debugpy-mcp"]
    }
  }
}
```

### Poetry

Poetry manages its own virtualenv. Use `poetry run` so Cursor doesn't need to know the venv path:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "poetry",
      "args": ["--directory", "/absolute/path/to/debugpy-mcp", "run", "debugpy-mcp"]
    }
  }
}
```

Alternatively, find the venv path with `poetry env info --path` and reference the binary directly:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "/path/from/poetry-env-info/bin/debugpy-mcp",
      "args": []
    }
  }
}
```

## Example workflow in Cursor

1. Ask the agent to run `debugpy_autodiscover_target` with `service_hint="api"`.
2. Ask the agent to run `debugpy_status`.
3. Ask the agent to run `debugpy_attach`.
4. Start your existing Attach configuration in Cursor.
5. Ask the agent to run `debugpy_context`, `debugpy_logs`, or `debugpy_breakpoint_plan`.

## Notes

### `debugpy` must exist inside the container

You need `debugpy` installed in the target image or container.

### PID attach may require ptrace permissions

If attach fails with `Operation not permitted`, the container may need additional capabilities such as:

- `cap_add: [SYS_PTRACE]`
- relaxed seccomp profile if required in your environment

### Gunicorn/Uvicorn worker setups

Attaching to a master process is often less useful than attaching to a worker process. The tool prefers uvicorn and worker processes first, but you can always pass a specific PID manually.

### Path mappings still matter

Your IDE attach config must map local source paths to remote container source paths.

Example:

```json
{
  "name": "Attach FastAPI in Docker",
  "type": "debugpy",
  "request": "attach",
  "connect": {
    "host": "localhost",
    "port": 5678
  },
  "pathMappings": [
    {
      "localRoot": "${workspaceFolder}",
      "remoteRoot": "/app"
    }
  ]
}
```

---

## DAP Session Tools

These tools implement full IDE-equivalent debugger control using the Debug Adapter Protocol (DAP). They talk directly to a running debugpy instance over TCP — no Docker access required for most operations.

> **Important:** debugpy accepts **only one DAP client at a time**. Disconnect your IDE (VS Code, Cursor, etc.) before using these tools, or your IDE session may be disrupted. If you need to verify that debugpy is listening while your IDE remains attached, use `debugpy_connect` — it performs a TCP-only connectivity check and does not displace the IDE session.

### Quick start

```python
# 1. Start the session (debugpy must already be listening)
debugpy_session_start(host="localhost", port=5678, container="my-api")

# 2. Set a breakpoint
debugpy_set_breakpoint(file="/Users/me/project/app/routes/users.py", line=42)

# 3. Trigger the code path in your app (send an HTTP request, etc.)
# 4. Once the breakpoint is hit, inspect state
debugpy_session_status()           # shows stopped_thread_id, stopped_frame_id
debugpy_threads()                   # full stack trace
debugpy_variables()                 # locals in current frame
debugpy_evaluate(expression="request.method")

# 5. Step through code
debugpy_step_over()
debugpy_step_in()
debugpy_step_out()

# 6. Resume
debugpy_continue()

# 7. Clean up
debugpy_session_stop()
```

### Session method

```bash
DEBUGPY_MCP_METHOD=persist debugpy-mcp     # keep connection alive (default)
DEBUGPY_MCP_METHOD=ephemeral debugpy-mcp   # reconnect on every tool call
```

In MCP config:
```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "debugpy-mcp",
      "env": { "DEBUGPY_MCP_METHOD": "persist" }
    }
  }
}
```

**When to use `ephemeral`:** If the MCP server is restarted frequently, or if you want guaranteed isolation between tool calls. Note that ephemeral mode preserves registered breakpoints and path mappings across reconnects — only the socket is recycled.

---

## Path Mapping Setup

Path mappings translate your local file paths (what you see in your editor) to the paths inside the container. Without them, breakpoints won't resolve correctly.

### Find the container source root

```bash
docker exec my-container pwd
# or
docker inspect my-container | grep WorkingDir
# or check the Dockerfile: WORKDIR /app
```

### Provide mappings manually

```python
debugpy_session_start(
    host="localhost",
    port=5678,
    path_mappings=[
        {"local_root": "/Users/me/project", "remote_root": "/app"}
    ]
)
```

### Auto-detection

If `container` is provided and Docker is accessible, the tool reads the container working directory and maps it to your local `cwd`. If `container` is omitted and the process is already stopped, it evaluates `os.getcwd()` inside the debuggee via DAP. Call `debugpy_session_status()` to see what was detected and override if needed.

### Verify breakpoints resolved

After `debugpy_set_breakpoint`, check `verified: true` in the response. Unverified breakpoints usually mean the path mapping is wrong or the module hasn't been imported yet.

---

## Multi-Container Debugging

Each `(host, port)` pair is an independent session. You can debug multiple services simultaneously:

```python
# API service on port 5678
debugpy_session_start(host="localhost", port=5678, container="api")
debugpy_set_breakpoint(file="app/routes/users.py", line=42, host="localhost", port=5678)

# Worker service on port 5679
debugpy_session_start(host="localhost", port=5679, container="worker")
debugpy_set_breakpoint(file="worker/tasks.py", line=15, host="localhost", port=5679)
```

Each service needs debugpy listening on its own port:
```yaml
# docker-compose.yml
services:
  api:
    ports: ["5678:5678"]
    command: python -m debugpy --listen 0.0.0.0:5678 -m uvicorn app.main:app
  worker:
    ports: ["5679:5678"]
    command: python -m debugpy --listen 0.0.0.0:5678 -m celery worker
```

---

## Thread Worker Setups (Gunicorn / Uvicorn)

When using Gunicorn, the master process spawns worker processes. **Always attach debugpy to a worker, not the master.** The master doesn't execute request handlers — the workers do.

**Recommended approach:** Start debugpy inside the worker with an env var or Gunicorn config hook:

```python
# gunicorn_config.py
def post_fork(server, worker):
    import debugpy
    debugpy.listen(("0.0.0.0", 5678))
```

**Identifying the right thread:** When a request comes in under uvicorn with multiple workers, use `debugpy_threads()` after pausing. Look for a thread whose stack includes your route handler (e.g., `handle_request`, `__call__`). The `file` and `line` fields in each frame identify exactly where each thread is executing.

```python
debugpy_pause()
debugpy_threads()
# Find thread whose frames show "app/routes/users.py"
debugpy_variables(frame_id=<frame_id_from_route_frame>)
```

---

## Future Architecture Options

### Option B — Asyncio DAP client

The current implementation uses a background `threading.Thread` to read DAP events and blocks tool-call threads waiting on `queue.Queue`. Migrating to `asyncio` would allow:

- **Proactive event handling:** React to `stopped` or `output` events automatically without a tool call triggering it (e.g., capture a variable snapshot every time any breakpoint is hit)
- **Cleaner concurrency model:** No locks on shared state; `asyncio` tasks are cooperative
- **Natural streaming:** Forward debuggee stdout/stderr to the agent in real time

Trade-off: Requires converting all tools to `async def` and replacing the reader thread with `asyncio` streams. FastMCP supports async tools natively, but all existing synchronous tools would need to be updated.

### Option C — Sidecar Process

Run a separate long-lived Python process that owns the DAP connection, exposing an IPC socket (e.g., a local Unix socket or a local TCP port). The MCP server tools talk to the sidecar via that socket.

Benefits:
- The DAP session **survives MCP server restarts** (e.g., when Claude Code reinitializes the server between conversations)
- Multiple MCP clients (different Claude sessions, different tools) can share one DAP session
- The sidecar can accumulate event history across sessions

Trade-off: Adds process management complexity. The sidecar must be started and monitored separately, and its lifecycle is decoupled from the MCP server.
