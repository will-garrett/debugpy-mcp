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
