# DAP Client Feature Design
**Date:** 2026-03-12
**Status:** Approved

## Overview

Add full IDE-equivalent debugger control to `debugpy-mcp` by implementing a DAP (Debug Adapter Protocol) client directly in the MCP server. This allows an AI agent to perform every action a developer would perform in a debugger IDE: set breakpoints, pause/resume/step execution, inspect variables, and evaluate expressions — all directed via MCP tool calls.

---

## Architecture

### Session Model

A module-level registry `_sessions: dict[tuple[str, int], DAPSession]` maps `(host, port)` pairs to live sessions. Each `(host, port)` pair represents one debugpy process (one container, one port). Multiple containers are handled by maintaining multiple entries.

The server gains a `--method` CLI flag (values: `persist`, `ephemeral`, default: `persist`).

**`DAPSession`** owns:
- A TCP socket to the debugpy DAP server
- A background `threading.Thread` running a read loop that parses incoming DAP messages and routes them:
  - Responses → `_pending: dict[int, queue.Queue]` (keyed by `request_seq`)
  - Events → `_event_queue: queue.Queue`
- Current state:
  - `connected: bool`
  - `stopped_thread_id: int | None`
  - `stopped_frame_id: int | None`
  - `breakpoints: list[DAPBreakpoint]` — all registered breakpoints across all files
  - `path_mappings: list[PathMapping]` — local↔remote path translation pairs
- Methods: `connect()`, `disconnect()`, `ensure_connected()`, `handshake()`

**Persist mode:** `ensure_connected()` checks if the socket is alive; reconnects + re-handshakes on drop. Sessions remain in `_sessions` across tool calls.

**Ephemeral mode:** `ensure_connected()` always opens a fresh socket. `disconnect()` is called at the end of every tool call. State (breakpoints, path mappings) is still stored on the session object across calls; only the socket is ephemeral.

### DAP Handshake

`DAPSession.handshake()` sends, in order:
1. `initialize` — identifies as `clientID: "debugpy-mcp"`, requests capabilities
2. `attach` — establishes this client as an independent observer (does not terminate debuggee on disconnect)
3. `configurationDone` — signals debugpy to resume if it was waiting

This is independent of any IDE already connected. debugpy supports multiple simultaneous clients.

### Message Protocol

DAP messages are newline-free JSON framed with an HTTP-style `Content-Length` header:
```
Content-Length: <n>\r\n
\r\n
{"seq": 1, "type": "request", "command": "...", "arguments": {...}}
```

The background reader thread parses this framing continuously. Tool calls send a request with a unique `seq` and block on `_pending[seq].get(timeout=DEFAULT_TIMEOUT)`.

---

## Tools

All new tools accept `host: str = "localhost"` and `port: int = DEFAULT_PORT`.

### Session Management

**`debugpy_session_start(host, port, path_mappings?)`**
- Connects, performs DAP handshake, stores session in `_sessions`
- If `path_mappings` not provided, runs auto-detection (see Path Mapping section)
- Returns session status, detected path mappings, and connection details

**`debugpy_session_stop(host, port)`**
- Sends DAP `disconnect` with `terminateDebuggee: false`
- Removes session from `_sessions`

**`debugpy_session_status(host, port)`**
- Returns: connection state, mode, `stopped_thread_id`, `stopped_frame_id`, registered breakpoints, path mappings

### Execution Control

**`debugpy_pause(host, port, thread_id?)`** → DAP `pause`

**`debugpy_continue(host, port, thread_id?)`** → DAP `continue`

**`debugpy_step_over(host, port, thread_id?)`** → DAP `next`

**`debugpy_step_in(host, port, thread_id?)`** → DAP `stepIn`

**`debugpy_step_out(host, port, thread_id?)`** → DAP `stepOut`

All execution control tools default `thread_id` to `session.stopped_thread_id` if not provided. They update `stopped_thread_id` and `stopped_frame_id` when a `stopped` event is received.

### Breakpoints

**`debugpy_set_breakpoint(host, port, file, line, condition?)`**
- Applies path mapping to translate `file` from local → remote
- Adds to `session.breakpoints`
- Sends full `setBreakpoints` list for that file to DAP (DAP requires the full list per file, not incremental)
- Returns breakpoint ID and verified status

**`debugpy_remove_breakpoint(host, port, breakpoint_id)`**
- Removes from `session.breakpoints`
- Re-sends updated `setBreakpoints` list for the affected file

**`debugpy_list_breakpoints(host, port)`**
- Returns all registered breakpoints with file (local path), line, condition, and DAP-verified status

### Inspection

**`debugpy_threads(host, port)`**
- DAP `threads` + `stackTrace` for each stopped thread
- Returns thread ID, name, state, and stack frames (frame ID, file, line, function name)

**`debugpy_variables(host, port, frame_id?, scope?)`**
- `scope`: `locals` | `globals` | `self` (default: `locals`)
- Defaults `frame_id` to `session.stopped_frame_id`
- DAP `scopes` → `variables` requests
- Returns name/value/type for each variable

**`debugpy_evaluate(host, port, expression, frame_id?, context?)`**
- `context`: `watch` | `repl` | `hover` (default: `watch`)
- Defaults `frame_id` to `session.stopped_frame_id`
- Returns result value, type, and any error message

---

## Path Mapping

Path mappings translate local file paths to container (remote) paths and back. Stored on the session as `list[PathMapping(local_root: str, remote_root: str)]`.

### Auto-Detection (runs in `debugpy_session_start` if no mappings provided)

1. Query the container's working dir via `readlink /proc/<pid>/cwd` (uses existing `get_working_dir`)
2. Check common roots: `/app`, `/code`, `/srv`, `/workspace`
3. Compare against local `cwd` of the MCP server process (`os.getcwd()`)
4. Return best-guess mappings; agent can inspect via `debugpy_session_status` and override

### Manual Config

`debugpy_session_start(path_mappings=[{"local_root": "/Users/me/project", "remote_root": "/app"}])`

### Breakpoint Translation

`debugpy_set_breakpoint` walks the mapping list and rewrites the `file` argument before sending to DAP. DAP responses (with remote paths) are translated back to local paths for all tool output.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Socket drops in persist mode | Background thread marks session `disconnected`; next tool call auto-reconnects once before returning error |
| DAP `success: false` response | Surface `message` field + contextual note in tool output |
| Stale frame/thread ID | Catch DAP error, suggest calling `debugpy_threads` for fresh IDs |
| Tool called without active session | Return error with suggestion to call `debugpy_session_start` first |
| Response timeout | Default 30s (matches `DEFAULT_TIMEOUT`); session marked disconnected on timeout |

---

## `--method` Flag

Added to the server entry point:

```
debugpy-mcp --method=persist    # default: keep DAP connection alive across calls
debugpy-mcp --method=ephemeral  # reconnect on every tool call
```

Stored as `SESSION_METHOD: Literal["persist", "ephemeral"]` module-level constant, read at startup.

---

## README Additions

1. **Full DAP session workflow** — start session → set breakpoint → trigger request in app → inspect stopped state → evaluate → continue
2. **Path mapping setup** — find container source root, manual vs auto-detect, verification via `debugpy_session_status`
3. **Multi-container debugging** — different `host:port` per service, independent sessions
4. **Thread worker setups** — attach to worker PID, use `debugpy_threads` to identify the executing thread; gunicorn/uvicorn notes
5. **`--method` flag** — when to use each mode
6. **Future architecture options:**
   - **Option B (asyncio):** Migrate when proactive event handling is needed — e.g., automatically capturing variable snapshots every time any breakpoint is hit, without a tool call triggering it. Requires converting all tools to `async def` and replacing the background thread with `asyncio` streams.
   - **Option C (sidecar process):** Run a separate long-lived process managing the DAP connection with IPC over a local socket. Useful when the MCP server restarts frequently (e.g., Claude Code reinitializing) and you want the DAP session to survive. Also allows sharing one session across multiple MCP clients.

---

## Testing

No new test files (consistent with existing codebase). All tools return structured output with `notes` and `next_steps` fields — the agent verifies behavior by reading tool responses. `debugpy_session_status` serves as the primary diagnostic tool.
