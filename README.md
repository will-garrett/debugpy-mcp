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

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run manually

```bash
debugpy-mcp
```

## Cursor MCP config

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

Or:

```json
{
  "mcpServers": {
    "debugpy-docker": {
      "command": "/absolute/path/to/debugpy-mcp/.venv/bin/python",
      "args": ["-m", "debugpy_mcp.server"]
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
