# DAP Client Feature Design
**Date:** 2026-03-12
**Status:** Approved

## Overview

Add full IDE-equivalent debugger control to `debugpy-mcp` by implementing a DAP (Debug Adapter Protocol) client directly in the MCP server. This allows an AI agent to perform every action a developer would perform in a debugger IDE: set breakpoints, pause/resume/step execution, inspect variables, and evaluate expressions — all directed via MCP tool calls.

---

## Architecture

### Session Model

A module-level registry `_sessions: dict[tuple[str, int], DAPSession]` maps `(host, port)` pairs to live sessions. Each `(host, port)` pair represents one debugpy process (one container, one port). Multiple containers are handled by maintaining multiple entries.

Session method is configured via the environment variable `DEBUGPY_MCP_METHOD` (values: `persist`, `ephemeral`, default: `persist`). This is consistent with how `DEFAULT_TIMEOUT`, `DEFAULT_PORT`, etc. are already configured in the codebase. Alternatively, a CLI wrapper can set this env var before invoking `mcp.run()`.

> **Note:** The `--method` flag as a CLI argument is intentionally avoided because FastMCP owns the CLI entry point via `mcp.run()`. Using an env var is the established pattern in this codebase and avoids conflicts.

**`DAPSession`** owns:
- A TCP socket to the debugpy DAP server
- A background `threading.Thread` running a read loop that parses incoming DAP messages and routes them:
  - Responses → `_pending: dict[int, queue.Queue]` (keyed by `request_seq`)
  - Events → `_event_queue: queue.Queue`
- A `threading.Lock` (`_state_lock`) protecting all mutable state fields written by the reader thread and read by tool calls
- Current state (all accesses through `_state_lock`):
  - `connected: bool`
  - `stopped_thread_id: int | None`
  - `stopped_frame_id: int | None` — populated by sending `stackTrace` for the stopped thread on each `stopped` event; caches the top frame's `frameId`
  - `breakpoints: list[DAPBreakpoint]` — all registered breakpoints across all files; each `DAPBreakpoint` carries an internal `internal_id` (a stable UUID assigned at set time) and the last DAP-assigned `dap_id`
  - `path_mappings: list[PathMapping]` — local↔remote path translation pairs
- Methods: `connect()`, `disconnect()`, `ensure_connected()`, `handshake()`

**Persist mode:** `ensure_connected()` checks if the socket is alive; reconnects + re-handshakes on drop. Sessions remain in `_sessions` across tool calls.

**Ephemeral mode:** `ensure_connected()` always opens a fresh socket. `disconnect()` is called at the end of every tool call. State (breakpoints, path mappings) is still stored on the session object across calls; only the socket is ephemeral. After each reconnect in ephemeral mode, breakpoints are re-sent via `setBreakpoints` and DAP-assigned IDs are refreshed on the `DAPBreakpoint` objects. The stable `internal_id` is always used in tool-facing APIs so agent-held IDs remain valid across reconnects.

### DAP Single-Client Constraint

**debugpy accepts only one DAP client connection at a time.** If a VS Code / Cursor IDE is already attached, `debugpy_session_start` will fail to connect or silently disrupt the existing IDE session. The agent and IDE cannot be connected simultaneously to the same debugpy port. `debugpy_session_start` detects a refused connection and returns a clear error noting this constraint.

### DAP Handshake

`DAPSession.handshake()` sends, in order:
1. `initialize` — identifies as `clientID: "debugpy-mcp"`, requests capabilities
2. `attach` — establishes this client as the DAP client (`terminateDebuggee: false` set on the session so `disconnect` does not kill the debuggee)
3. `configurationDone` — required by DAP protocol after `attach`. If the target was started with `--wait-for-client`, this releases it. If the target is already running, this is a no-op. It does not unconditionally resume a paused process; it only completes the configuration phase.

### Stopped Frame Resolution

The background reader thread **must not** send DAP requests (e.g., `stackTrace`) because it is the only entity reading from the socket. Doing so would deadlock: the reader would block waiting for a response it can never deliver to itself.

Instead:
1. The reader thread receives a `stopped` event, records `stopped_thread_id` under `_state_lock`, and enqueues the raw event onto `_event_queue`
2. Execution control tools (`debugpy_pause`, `debugpy_step_over`, etc.) send their DAP request then block on `_event_queue.get(timeout=DEFAULT_TIMEOUT)` waiting for a `stopped` event
3. Once the tool call thread receives the `stopped` event, it sends `stackTrace` for `stopped_thread_id` and caches the top frame's `frameId` as `stopped_frame_id` (under `_state_lock`)

For cases where a `stopped` event arrives spontaneously (e.g., hitting a breakpoint while the process is running between tool calls), the event accumulates in `_event_queue`. The next tool call that checks session state will drain the queue and update `stopped_thread_id` / `stopped_frame_id` at that point via a dedicated `_sync_stopped_state()` helper that sends `stackTrace` if the queue is non-empty.

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

**`debugpy_session_start(host, port, path_mappings?, container?)`**
- Connects, performs DAP handshake, stores session in `_sessions`
- `container`: optional Docker container name. If provided, enables Docker-based path auto-detection. If omitted, falls back to DAP-based detection (see Path Mapping section)
- If `path_mappings` not provided, runs auto-detection
- Returns session status, detected path mappings, and connection details
- On connection refused: returns error noting the single-client constraint

**`debugpy_session_stop(host, port)`**
- Sends DAP `disconnect` with `terminateDebuggee: false`
- Removes session from `_sessions`

**`debugpy_session_status(host, port)`**
- Returns: connection state, mode, `stopped_thread_id`, `stopped_frame_id`, registered breakpoints (with `internal_id`s), path mappings

### Execution Control

**`debugpy_pause(host, port, thread_id?)`** → DAP `pause`

**`debugpy_continue(host, port, thread_id?)`** → DAP `continue`

**`debugpy_step_over(host, port, thread_id?)`** → DAP `next`

**`debugpy_step_in(host, port, thread_id?)`** → DAP `stepIn`

**`debugpy_step_out(host, port, thread_id?)`** → DAP `stepOut`

All execution control tools default `thread_id` to `session.stopped_thread_id` if not provided. On receiving the subsequent `stopped` event (delivered via the background reader), the session updates `stopped_thread_id` and `stopped_frame_id` automatically.

### Breakpoints

**`debugpy_set_breakpoint(host, port, file, line, condition?)`**
- Applies path mapping to translate `file` from local → remote
- Creates a `DAPBreakpoint` with a stable `internal_id` (UUID)
- Adds to `session.breakpoints`
- Sends full `setBreakpoints` list for that file to DAP (DAP requires the full list per file, not incremental)
- Stores DAP-assigned `dap_id` on the breakpoint object
- Returns `internal_id` and verified status to the agent

**`debugpy_remove_breakpoint(host, port, breakpoint_id)`**
- `breakpoint_id` is the `internal_id` (stable UUID, safe across reconnects)
- Removes from `session.breakpoints`
- Re-sends updated `setBreakpoints` list for the affected file

**`debugpy_list_breakpoints(host, port)`**
- Returns all registered breakpoints with `internal_id`, file (local path), line, condition, and DAP-verified status

### Inspection

**`debugpy_threads(host, port)`**
- DAP `threads` + `stackTrace` for each stopped thread
- Returns thread ID, name, state, and stack frames (frame ID, file, line, function name)

**`debugpy_variables(host, port, frame_id?, scope?)`**
- `scope`: `locals` | `globals` (default: `locals`)
- Defaults `frame_id` to `session.stopped_frame_id`
- DAP `scopes` → `variables` requests; scope name matched case-insensitively against DAP-returned scope names
- Returns name/value/type for each variable

**`debugpy_evaluate(host, port, expression, frame_id?, context?)`**
- `context`: `watch` | `repl` | `hover` (default: `watch`)
- Defaults `frame_id` to `session.stopped_frame_id`
- Returns result value, type, and any error message

---

## Path Mapping

Path mappings translate local file paths to container (remote) paths and back. Stored on the session as `list[PathMapping(local_root: str, remote_root: str)]`.

### Auto-Detection Strategy

Two strategies, tried in order:

**Strategy 1 — Docker-based (requires `container` arg in `debugpy_session_start`):**
1. Query the container's working dir via `readlink /proc/<pid>/cwd` (uses existing `get_working_dir`)
2. Check common roots: `/app`, `/code`, `/srv`, `/workspace`
3. Compare against local `cwd` of the MCP server process (`os.getcwd()`)

**Strategy 2 — DAP-based (no Docker required, works for remote VMs etc.):**
1. Send a DAP `evaluate` request with expression `__import__('os').getcwd()` in `repl` context, **without** a `frameId`
2. This only succeeds if the target is currently stopped (at a breakpoint or `--wait-for-client`). If the target is running, debugpy returns an error — this is a normal, non-fatal outcome
3. If successful, use the returned path as the remote root and compare against local `cwd`
4. If the evaluate fails (process running), Strategy 2 yields no mappings; the tool returns a note: "Auto-detection via DAP requires a stopped process. Provide path_mappings manually or call debugpy_session_start again after hitting a breakpoint."

Both strategies return best-guess mappings. Agent can inspect via `debugpy_session_status` and override.

### Manual Config

`debugpy_session_start(path_mappings=[{"local_root": "/Users/me/project", "remote_root": "/app"}])`

### Breakpoint Translation

`debugpy_set_breakpoint` walks the mapping list and rewrites the `file` argument before sending to DAP. DAP responses (with remote paths) are translated back to local paths for all tool output.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Connection refused (IDE already connected) | Return error: "Connection refused — debugpy accepts only one DAP client at a time. Disconnect your IDE first." |
| Socket drops in persist mode | Background thread marks session `disconnected`; next tool call auto-reconnects once before returning error |
| DAP `success: false` response | Surface `message` field + contextual note in tool output |
| Stale frame/thread ID | Catch DAP error, suggest calling `debugpy_threads` for fresh IDs |
| Tool called without active session | Return error with suggestion to call `debugpy_session_start` first |
| Response timeout | Default 30s (matches `DEFAULT_TIMEOUT`); session marked disconnected on timeout |
| Ephemeral reconnect breakpoint re-sync | On reconnect, re-send all `setBreakpoints` lists; update `dap_id` on each `DAPBreakpoint`; `internal_id` unchanged |

---

## Session Method Configuration

Configured via environment variable `DEBUGPY_MCP_METHOD` (consistent with existing env-var pattern):

```bash
DEBUGPY_MCP_METHOD=persist debugpy-mcp      # default: keep DAP connection alive
DEBUGPY_MCP_METHOD=ephemeral debugpy-mcp    # reconnect on every tool call
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

---

## README Additions

1. **Full DAP session workflow** — start session → set breakpoint → trigger request in app → inspect stopped state → evaluate → continue
2. **Path mapping setup** — find container source root, manual vs auto-detect, verification via `debugpy_session_status`; note that `container` arg enables Docker-based detection while omitting it uses DAP-based detection
3. **Multi-container debugging** — different `host:port` per service, independent sessions, each with their own path mappings
4. **Thread worker setups** — attach to worker PID, use `debugpy_threads` to identify the executing thread; gunicorn/uvicorn notes
5. **`DEBUGPY_MCP_METHOD`** — when to use `persist` vs `ephemeral`
6. **Single-client constraint** — debugpy accepts one DAP client at a time; disconnect IDE before using MCP DAP tools, or use `debugpy_connect` (TCP check only) when IDE is attached
7. **Future architecture options:**
   - **Option B (asyncio):** Migrate when proactive event handling is needed — e.g., automatically capturing variable snapshots every time any breakpoint is hit, without a tool call triggering it. Requires converting all tools to `async def` and replacing the background thread with `asyncio` streams.
   - **Option C (sidecar process):** Run a separate long-lived process managing the DAP connection with IPC over a local socket. Useful when the MCP server restarts frequently (e.g., Claude Code reinitializing) and you want the DAP session to survive. Also allows sharing one session across multiple MCP clients.

---

## Testing

No new test files (consistent with existing codebase). All tools return structured output with `notes` and `next_steps` fields — the agent verifies behavior by reading tool responses. `debugpy_session_status` serves as the primary diagnostic tool.
