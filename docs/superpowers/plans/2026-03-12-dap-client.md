# DAP Client Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full IDE-equivalent debugger control (breakpoints, step/pause/resume, variable inspection, expression evaluation) to debugpy-mcp via a DAP protocol client.

**Architecture:** A `DAPSession` class in a new `dap.py` module manages one TCP connection per `(host, port)` target; a background reader thread routes DAP responses and events; execution-control tool-call threads drain stopped events and issue stackTrace. All new MCP tools live in the existing `server.py`.

**Tech Stack:** Python stdlib only — `socket`, `threading`, `queue`, `json`, `uuid`. No new dependencies. FastMCP + Pydantic (already present).

**Spec:** `docs/superpowers/specs/2026-03-12-dap-client-design.md`

**Note on tests:** The existing codebase has no test suite. Per the spec, tools return rich structured output with `notes` and `next_steps` fields. Manual verification steps use tool output as the oracle.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/debugpy_mcp/dap.py` | **Create** | DAPSession class, PathMapping, DAPBreakpoint, all DAP protocol logic |
| `src/debugpy_mcp/server.py` | **Modify** | Import dap.py, add SESSION_METHOD env var, add 12 new MCP tools |
| `README.md` | **Modify** | New sections: DAP workflow, path mapping, multi-container, thread workers, method config, future options |

---

## Chunk 1: DAP Infrastructure (`dap.py`)

### Task 1: Create `dap.py` with data models and session skeleton

**Files:**
- Create: `src/debugpy_mcp/dap.py`

- [ ] **Step 1: Create `dap.py` with imports, data models, and DAPSession skeleton**

```python
# src/debugpy_mcp/dap.py
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import uuid
from typing import Any, Literal


class PathMapping:
    """Maps a local source root to a remote (container) source root."""

    def __init__(self, local_root: str, remote_root: str) -> None:
        self.local_root = local_root.rstrip("/")
        self.remote_root = remote_root.rstrip("/")

    def to_remote(self, local_path: str) -> str:
        if local_path.startswith(self.local_root):
            return self.remote_root + local_path[len(self.local_root):]
        return local_path

    def to_local(self, remote_path: str) -> str:
        if remote_path.startswith(self.remote_root):
            return self.local_root + remote_path[len(self.remote_root):]
        return remote_path

    def to_dict(self) -> dict[str, str]:
        return {"local_root": self.local_root, "remote_root": self.remote_root}


class DAPBreakpoint:
    """Represents one registered breakpoint with a stable internal ID."""

    def __init__(self, file: str, line: int, condition: str | None = None) -> None:
        self.internal_id: str = str(uuid.uuid4())
        self.file = file          # local path (as given by caller)
        self.line = line
        self.condition = condition
        self.dap_id: int | None = None      # assigned by debugpy after setBreakpoints
        self.verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.internal_id,
            "file": self.file,
            "line": self.line,
            "condition": self.condition,
            "verified": self.verified,
            "dap_id": self.dap_id,
        }


class DAPSession:
    """Manages one DAP connection to a running debugpy process."""

    def __init__(self, host: str, port: int, method: Literal["persist", "ephemeral"]) -> None:
        self.host = host
        self.port = port
        self.method = method

        # Socket and reader thread
        self._sock: socket.socket | None = None
        self._buf: bytes = b""
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

        # Response routing: seq → Queue that the caller blocks on
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        # Async event queue (stopped, output, breakpoint, etc.)
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        # Mutable state — always access under _state_lock
        self._state_lock = threading.Lock()
        self._connected: bool = False
        self._seq: int = 0
        self.stopped_thread_id: int | None = None
        self.stopped_frame_id: int | None = None

        # Persistent state (survives ephemeral reconnects)
        self.breakpoints: list[DAPBreakpoint] = []
        self.path_mappings: list[PathMapping] = []

    # ------------------------------------------------------------------
    # Public state accessors (thread-safe)
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def _set_connected(self, value: bool) -> None:
        with self._state_lock:
            self._connected = value

    def _next_seq(self) -> int:
        with self._state_lock:
            self._seq += 1
            return self._seq
```

- [ ] **Step 2: Verify file created with no syntax errors**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "import src.debugpy_mcp.dap"
```
Expected: no output (clean import)

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/dap.py && git commit -m "feat: add dap.py with DAPSession skeleton, PathMapping, DAPBreakpoint"
```

---

### Task 2: DAP message framing and reader thread

**Files:**
- Modify: `src/debugpy_mcp/dap.py`

- [ ] **Step 1: Add `_send_msg`, `_read_msg_raw`, and background reader thread to `DAPSession`**

Add these methods to `DAPSession` (before the closing of the class):

```python
    # ------------------------------------------------------------------
    # Message framing
    # ------------------------------------------------------------------

    def _send_msg(self, msg: dict[str, Any]) -> None:
        """Encode and send one DAP message. Called from tool-call threads."""
        body = json.dumps(msg).encode()
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        assert self._sock is not None
        self._sock.sendall(frame)

    def _read_msg_raw(self) -> dict[str, Any]:
        """Read one complete DAP message from the socket. Called ONLY from reader thread."""
        assert self._sock is not None
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("DAP socket closed")
            self._buf += chunk
        sep = self._buf.index(b"\r\n\r\n")
        header_bytes, self._buf = self._buf[:sep], self._buf[sep + 4:]
        length = int(
            next(
                line.split(":", 1)[1].strip()
                for line in header_bytes.decode().split("\r\n")
                if line.lower().startswith("content-length")
            )
        )
        while len(self._buf) < length:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("DAP socket closed")
            self._buf += chunk
        body, self._buf = self._buf[:length], self._buf[length:]
        return json.loads(body)

    # ------------------------------------------------------------------
    # Background reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Runs in a background thread. Routes responses to callers and events to queue."""
        try:
            while not self._reader_stop.is_set():
                try:
                    msg = self._read_msg_raw()
                except (ConnectionError, OSError, StopIteration, ValueError):
                    break
                msg_type = msg.get("type")
                if msg_type == "response":
                    seq = msg.get("request_seq")
                    with self._pending_lock:
                        q = self._pending.get(seq)
                    if q is not None:
                        q.put(msg)
                elif msg_type == "event":
                    event = msg.get("event")
                    if event == "stopped":
                        thread_id = msg.get("body", {}).get("threadId")
                        with self._state_lock:
                            self.stopped_thread_id = thread_id
                    # Skip internal DAP housekeeping events that are not useful to tools
                    if event not in ("initialized", "process", "module", "loadedSource"):
                        self._event_queue.put(msg)
        finally:
            self._set_connected(False)

    def _start_reader(self) -> None:
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True, name=f"dap-reader-{self.host}:{self.port}")
        self._reader.start()
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "import src.debugpy_mcp.dap; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/dap.py && git commit -m "feat: add DAP message framing and background reader thread"
```

---

### Task 3: Request helper, connect/disconnect, and handshake

**Files:**
- Modify: `src/debugpy_mcp/dap.py`

- [ ] **Step 1: Add `_request`, `connect`, `disconnect`, `ensure_connected`, and `handshake`**

Add these methods to `DAPSession`:

```python
    # ------------------------------------------------------------------
    # Request / response
    # ------------------------------------------------------------------

    def _request(self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Send a DAP request and block until the matching response arrives."""
        seq = self._next_seq()
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._pending_lock:
            self._pending[seq] = response_queue
        msg: dict[str, Any] = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        self._send_msg(msg)
        try:
            return response_queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"DAP '{command}' request timed out after {timeout}s")
        finally:
            with self._pending_lock:
                self._pending.pop(seq, None)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """Open the TCP socket and start the reader thread."""
        self._buf = b""
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.settimeout(None)  # blocking reads in reader thread
        self._start_reader()
        self._set_connected(True)

    def disconnect(self, terminate_debuggee: bool = False) -> None:
        """Send DAP disconnect and close the socket."""
        if self._sock is not None:
            try:
                self._request("disconnect", {"restart": False, "terminateDebuggee": terminate_debuggee}, timeout=5.0)
            except Exception:
                pass
        self._reader_stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._set_connected(False)

    def ensure_connected(self, timeout: float = 30.0) -> None:
        """Ensure a live connection exists. In ephemeral mode, always reconnects."""
        if self.method == "ephemeral":
            if self._sock is not None:
                self.disconnect()
            self.connect()
            self.handshake(timeout=timeout)
            return
        # persist mode: reconnect if dropped
        if not self.connected:
            self.connect()
            self.handshake(timeout=timeout)
            self._resync_breakpoints(timeout=timeout)

    def ensure_disconnected_if_ephemeral(self) -> None:
        """In ephemeral mode, disconnect after each tool call."""
        if self.method == "ephemeral":
            self.disconnect()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def handshake(self, timeout: float = 30.0) -> None:
        """Perform the DAP initialize → attach → configurationDone sequence."""
        init_resp = self._request("initialize", {
            "clientID": "debugpy-mcp",
            "clientName": "debugpy-mcp",
            "adapterID": "debugpy",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": "path",
            "supportsVariableType": True,
            "supportsEvaluateForHovers": True,
        }, timeout=timeout)
        if not init_resp.get("success"):
            raise ConnectionError(f"DAP initialize failed: {init_resp.get('message', init_resp)}")

        attach_resp = self._request("attach", {
            "justMyCode": False,
            "subProcess": False,
        }, timeout=timeout)
        if not attach_resp.get("success"):
            raise ConnectionError(f"DAP attach failed: {attach_resp.get('message', attach_resp)}")

        self._request("configurationDone", timeout=timeout)
```

- [ ] **Step 2: Verify syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "import src.debugpy_mcp.dap; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/dap.py && git commit -m "feat: add DAP request helper, connect/disconnect lifecycle, handshake"
```

---

### Task 4: Stopped state sync, path mapping helpers, and breakpoint re-sync

**Files:**
- Modify: `src/debugpy_mcp/dap.py`

- [ ] **Step 1: Add `_sync_stopped_state`, path mapping helpers, and `_resync_breakpoints`**

Add to `DAPSession`:

```python
    # ------------------------------------------------------------------
    # Stopped state resolution
    # ------------------------------------------------------------------

    def _sync_stopped_state(self, timeout: float = 5.0) -> None:
        """Drain any pending stopped events and update stopped_thread_id / stopped_frame_id."""
        # Drain events without blocking if the queue is empty
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if event.get("event") == "stopped":
                thread_id = event.get("body", {}).get("threadId")
                if thread_id is not None:
                    with self._state_lock:
                        self.stopped_thread_id = thread_id

        # If we have a stopped thread, fetch the top frame
        with self._state_lock:
            thread_id = self.stopped_thread_id
        if thread_id is not None:
            try:
                resp = self._request("stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 1}, timeout=timeout)
                frames = resp.get("body", {}).get("stackFrames", [])
                if frames:
                    with self._state_lock:
                        self.stopped_frame_id = frames[0]["id"]
            except Exception:
                pass

    def wait_for_stop(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Block until a stopped event arrives (used by step/pause tools).

        Collects non-stopped events in a local buffer and re-enqueues them after
        the loop exits, avoiding a busy-wait spin on a non-empty queue.
        """
        import time
        deadline = time.monotonic() + timeout
        non_stopped: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = self._event_queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if event.get("event") == "stopped":
                    result = event
                    break
                # Not a stopped event — buffer it to re-enqueue later
                non_stopped.append(event)
        finally:
            for e in non_stopped:
                self._event_queue.put(e)

        if result is not None:
            thread_id = result.get("body", {}).get("threadId")
            with self._state_lock:
                self.stopped_thread_id = thread_id
            self._sync_stopped_state(timeout=5.0)
        return result

    # ------------------------------------------------------------------
    # Path mapping helpers
    # ------------------------------------------------------------------

    def to_remote_path(self, local_path: str) -> str:
        for m in self.path_mappings:
            remote = m.to_remote(local_path)
            if remote != local_path:
                return remote
        return local_path

    def to_local_path(self, remote_path: str) -> str:
        for m in self.path_mappings:
            local = m.to_local(remote_path)
            if local != remote_path:
                return local
        return remote_path

    # ------------------------------------------------------------------
    # Breakpoint re-sync (called after reconnect)
    # ------------------------------------------------------------------

    def _resync_breakpoints(self, timeout: float = 30.0) -> None:
        """Re-send all registered breakpoints to DAP after reconnect."""
        files: dict[str, list[DAPBreakpoint]] = {}
        for bp in self.breakpoints:
            files.setdefault(bp.file, []).append(bp)
        for local_file, bps in files.items():
            remote_file = self.to_remote_path(local_file)
            source_bps = [{"line": bp.line, **({"condition": bp.condition} if bp.condition else {})} for bp in bps]
            try:
                resp = self._request("setBreakpoints", {
                    "source": {"path": remote_file},
                    "breakpoints": source_bps,
                }, timeout=timeout)
                dap_bps = resp.get("body", {}).get("breakpoints", [])
                for bp, dap_bp in zip(bps, dap_bps):
                    bp.dap_id = dap_bp.get("id")
                    bp.verified = dap_bp.get("verified", False)
            except Exception:
                pass
```

- [ ] **Step 2: Verify syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "import src.debugpy_mcp.dap; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/dap.py && git commit -m "feat: add stopped state sync, path mapping helpers, breakpoint re-sync"
```

---

### Task 5: Path auto-detection strategies

**Files:**
- Modify: `src/debugpy_mcp/dap.py`

- [ ] **Step 1: Add module-level `detect_path_mappings` function**

Add after the `DAPSession` class (at module level). Note: `os` is already imported at the top of the file.

```python
import subprocess as _subprocess

COMMON_REMOTE_ROOTS = ["/app", "/code", "/srv", "/workspace", "/home/app"]


def detect_path_mappings(
    session: DAPSession,
    container: str | None,
    timeout: float = 10.0,
) -> tuple[list[PathMapping], list[str]]:
    """
    Try to auto-detect path mappings.

    Strategy 1 (Docker-based, requires container name): read /proc/1/cwd.
    Strategy 2 (DAP-based, requires stopped process): evaluate os.getcwd() via DAP repl.

    Returns (mappings, notes).
    """
    notes: list[str] = []
    local_root = os.getcwd()

    # Strategy 1: Docker-based
    if container:
        try:
            proc = _subprocess.run(
                ["docker", "exec", container, "sh", "-lc", "readlink /proc/1/cwd 2>/dev/null || cat /proc/1/cwd 2>/dev/null"],
                capture_output=True, text=True, timeout=10,
            )
            remote_root = proc.stdout.strip()
            if not remote_root:
                # Try common roots
                for root in COMMON_REMOTE_ROOTS:
                    check = _subprocess.run(
                        ["docker", "exec", container, "sh", "-lc", f"test -d {root} && echo yes"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if check.stdout.strip() == "yes":
                        remote_root = root
                        break
            if remote_root:
                notes.append(f"Auto-detected via Docker: {local_root} → {remote_root}")
                return [PathMapping(local_root, remote_root)], notes
            notes.append("Docker-based detection found no working directory; falling through to DAP strategy.")
        except Exception as exc:
            notes.append(f"Docker-based detection failed ({exc}); falling through to DAP strategy.")

    # Strategy 2: DAP evaluate (only works if process is stopped)
    try:
        resp = session._request("evaluate", {
            "expression": "__import__('os').getcwd()",
            "context": "repl",
        }, timeout=timeout)
        if resp.get("success"):
            remote_root = resp.get("body", {}).get("result", "").strip("'\"")
            if remote_root:
                notes.append(f"Auto-detected via DAP: {local_root} → {remote_root}")
                return [PathMapping(local_root, remote_root)], notes
    except Exception:
        pass

    notes.append(
        "Auto-detection could not determine path mappings. "
        "Process may be running (not stopped). "
        "Provide path_mappings manually or call debugpy_session_start again after hitting a breakpoint."
    )
    return [], notes
```

- [ ] **Step 2: Verify syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "import src.debugpy_mcp.dap; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/dap.py && git commit -m "feat: add path mapping auto-detection (Docker + DAP strategies)"
```

---

## Chunk 2: MCP Tools (`server.py`)

### Task 6: Session registry and session management tools

**Files:**
- Modify: `src/debugpy_mcp/server.py`

- [ ] **Step 1: Add imports and session registry to `server.py`**

At the top of `server.py`, after the existing imports, add:

```python
from debugpy_mcp.dap import DAPSession, DAPBreakpoint, PathMapping, detect_path_mappings
```

Note: `Literal` is already imported on line 8 of `server.py` (`from typing import Any, Literal`). Do not add a duplicate.

And after the existing DEFAULT_* constants block, add:

```python
SESSION_METHOD: Literal["persist", "ephemeral"] = os.getenv("DEBUGPY_MCP_METHOD", "persist")  # type: ignore[assignment]
_sessions: dict[tuple[str, int], DAPSession] = {}


def _get_session(host: str, port: int) -> DAPSession:
    """Return existing session or raise ToolError."""
    key = (host, port)
    if key not in _sessions:
        raise ToolError(
            f"No active DAP session for {host}:{port}. "
            "Call debugpy_session_start first."
        )
    return _sessions[key]


def _require_session(host: str, port: int) -> DAPSession:
    """Return session after syncing stopped state."""
    session = _get_session(host, port)
    session.ensure_connected()
    session._sync_stopped_state()
    return session
```

- [ ] **Step 2: Add session management tools**

Append to `server.py` (before `main()`):

```python
# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class SessionStatusResult(BaseModel):
    ok: bool
    host: str
    port: int
    method: str
    connected: bool
    stopped_thread_id: int | None = None
    stopped_frame_id: int | None = None
    breakpoints: list[dict] = Field(default_factory=list)
    path_mappings: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@mcp.tool()
def debugpy_session_start(
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    path_mappings: list[dict] | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    """Start a DAP session with a running debugpy process.

    path_mappings: list of {"local_root": "...", "remote_root": "..."} dicts.
    container: Docker container name for Docker-based path auto-detection.
    If path_mappings is omitted, auto-detection is attempted.
    IMPORTANT: debugpy accepts only one DAP client at a time.
    Disconnect any IDE before calling this tool.
    """
    key = (host, port)
    notes: list[str] = []

    # Close any existing session for this key
    if key in _sessions:
        try:
            _sessions[key].disconnect()
        except Exception:
            pass
        del _sessions[key]

    session = DAPSession(host=host, port=port, method=SESSION_METHOD)

    try:
        session.connect()
    except ConnectionRefusedError:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[
                f"Connection refused at {host}:{port}.",
                "debugpy accepts only one DAP client at a time. Disconnect your IDE first.",
                "Or verify debugpy is listening: use debugpy_connect to check.",
            ],
        ).model_dump()
    except OSError as exc:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"Connection error: {exc}"],
        ).model_dump()

    try:
        session.handshake()
    except ConnectionError as exc:
        session.disconnect()
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"DAP handshake failed: {exc}"],
        ).model_dump()

    # Path mappings
    if path_mappings:
        session.path_mappings = [PathMapping(m["local_root"], m["remote_root"]) for m in path_mappings]
        notes.append(f"Using {len(session.path_mappings)} provided path mapping(s).")
    else:
        detected, detect_notes = detect_path_mappings(session, container=container)
        session.path_mappings = detected
        notes.extend(detect_notes)

    _sessions[key] = session
    session._sync_stopped_state()
    notes.append(f"Session started in '{SESSION_METHOD}' mode.")

    return SessionStatusResult(
        ok=True, host=host, port=port, method=SESSION_METHOD, connected=True,
        stopped_thread_id=session.stopped_thread_id,
        stopped_frame_id=session.stopped_frame_id,
        breakpoints=[bp.to_dict() for bp in session.breakpoints],
        path_mappings=[m.to_dict() for m in session.path_mappings],
        notes=notes,
    ).model_dump()


@mcp.tool()
def debugpy_session_stop(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Cleanly disconnect the DAP session. Does not terminate the debuggee."""
    key = (host, port)
    if key not in _sessions:
        return {"ok": False, "notes": [f"No active session for {host}:{port}."]}
    try:
        _sessions[key].disconnect(terminate_debuggee=False)
    except Exception as exc:
        return {"ok": False, "notes": [f"Disconnect error: {exc}"]}
    finally:
        del _sessions[key]
    return {"ok": True, "notes": [f"Session {host}:{port} stopped."]}


@mcp.tool()
def debugpy_session_status(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return the current state of a DAP session: connection, stopped position, breakpoints, mappings."""
    key = (host, port)
    if key not in _sessions:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"No active session for {host}:{port}. Call debugpy_session_start first."],
        ).model_dump()
    session = _sessions[key]
    session._sync_stopped_state()
    return SessionStatusResult(
        ok=True, host=host, port=port, method=SESSION_METHOD,
        connected=session.connected,
        stopped_thread_id=session.stopped_thread_id,
        stopped_frame_id=session.stopped_frame_id,
        breakpoints=[bp.to_dict() for bp in session.breakpoints],
        path_mappings=[m.to_dict() for m in session.path_mappings],
        notes=[],
    ).model_dump()
```

- [ ] **Step 3: Verify import and syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "from debugpy_mcp.server import debugpy_session_start; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/server.py && git commit -m "feat: add session registry and session management MCP tools"
```

---

### Task 7: Execution control tools

**Files:**
- Modify: `src/debugpy_mcp/server.py`

- [ ] **Step 1: Add execution control tools**

Append to `server.py` (before `main()`):

```python
# ---------------------------------------------------------------------------
# Execution control
# ---------------------------------------------------------------------------

class ExecControlResult(BaseModel):
    ok: bool
    host: str
    port: int
    command: str
    stopped: bool = False
    stopped_thread_id: int | None = None
    stopped_frame_id: int | None = None
    stopped_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


def _exec_control(session: DAPSession, host: str, port: int, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Shared implementation for execution control tools. Caller is responsible for obtaining the session."""
    try:
        resp = session._request(command, arguments)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(ok=False, host=host, port=port, command=command, notes=[f"DAP error: {exc}"]).model_dump()

    if not resp.get("success"):
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(
            ok=False, host=host, port=port, command=command,
            notes=[resp.get("message", "DAP request failed"), "Thread may no longer be stopped."],
        ).model_dump()

    # Wait for stopped event (step/pause commands produce one)
    stopped_event = session.wait_for_stop(timeout=30.0)
    session.ensure_disconnected_if_ephemeral()

    if stopped_event:
        reason = stopped_event.get("body", {}).get("reason", "unknown")
        return ExecControlResult(
            ok=True, host=host, port=port, command=command, stopped=True,
            stopped_thread_id=session.stopped_thread_id,
            stopped_frame_id=session.stopped_frame_id,
            stopped_reason=reason,
        ).model_dump()
    return ExecControlResult(
        ok=True, host=host, port=port, command=command, stopped=False,
        notes=["Command sent; no stopped event received within timeout. Process may still be running."],
    ).model_dump()


@mcp.tool()
def debugpy_pause(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Pause execution. Defaults to all threads."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return ExecControlResult(ok=False, host=host, port=port, command="pause", notes=[str(exc)]).model_dump()
    args: dict[str, Any] = {}
    if thread_id is not None:
        args["threadId"] = thread_id
    return _exec_control(session, host, port, "pause", args)


@mcp.tool()
def debugpy_continue(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Resume execution."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}
    args: dict[str, Any] = {}
    if thread_id is not None:
        args["threadId"] = thread_id
    elif session.stopped_thread_id is not None:
        args["threadId"] = session.stopped_thread_id
    try:
        resp = session._request("continue", args)
        with session._state_lock:
            session.stopped_thread_id = None
            session.stopped_frame_id = None
        session.ensure_disconnected_if_ephemeral()
        return {"ok": resp.get("success", True), "host": host, "port": port, "notes": ["Resumed."]}
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}"]}


def _step_tool(host: str, port: int, command: str, thread_id: int | None) -> dict[str, Any]:
    """Shared wrapper for step_over / step_in / step_out."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return ExecControlResult(ok=False, host=host, port=port, command=command, notes=[str(exc)]).model_dump()
    tid = thread_id if thread_id is not None else session.stopped_thread_id
    if tid is None:
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(
            ok=False, host=host, port=port, command=command,
            notes=["No stopped thread. Use debugpy_pause first or wait for a breakpoint."],
        ).model_dump()
    return _exec_control(session, host, port, command, {"threadId": tid})


@mcp.tool()
def debugpy_step_over(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step over the current line (DAP 'next')."""
    return _step_tool(host, port, "next", thread_id)


@mcp.tool()
def debugpy_step_in(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step into the next function call (DAP 'stepIn')."""
    return _step_tool(host, port, "stepIn", thread_id)


@mcp.tool()
def debugpy_step_out(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step out of the current function (DAP 'stepOut')."""
    return _step_tool(host, port, "stepOut", thread_id)
```

- [ ] **Step 2: Verify syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "from debugpy_mcp.server import debugpy_step_over; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/server.py && git commit -m "feat: add execution control MCP tools (pause/continue/step)"
```

---

### Task 8: Breakpoint tools

**Files:**
- Modify: `src/debugpy_mcp/server.py`

- [ ] **Step 1: Add breakpoint tools**

Append to `server.py` (before `main()`):

```python
# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------

def _send_breakpoints_for_file(session: DAPSession, local_file: str, timeout: float = 30.0) -> list[dict]:
    """Send the full setBreakpoints list for one file. Returns raw DAP breakpoint dicts."""
    remote_file = session.to_remote_path(local_file)
    bps_for_file = [bp for bp in session.breakpoints if bp.file == local_file]
    source_bps = [
        {"line": bp.line, **({"condition": bp.condition} if bp.condition else {})}
        for bp in bps_for_file
    ]
    resp = session._request("setBreakpoints", {
        "source": {"path": remote_file},
        "breakpoints": source_bps,
    }, timeout=timeout)
    dap_bps = resp.get("body", {}).get("breakpoints", [])
    for bp, dap_bp in zip(bps_for_file, dap_bps):
        bp.dap_id = dap_bp.get("id")
        bp.verified = dap_bp.get("verified", False)
    return dap_bps


@mcp.tool()
def debugpy_set_breakpoint(
    file: str,
    line: int,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    condition: str | None = None,
) -> dict[str, Any]:
    """Set a breakpoint at file:line. file should be a local path; path mapping is applied automatically.

    Returns a breakpoint ID (use with debugpy_remove_breakpoint).
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    bp = DAPBreakpoint(file=file, line=line, condition=condition)
    session.breakpoints.append(bp)

    try:
        _send_breakpoints_for_file(session, file)
    except Exception as exc:
        session.breakpoints.remove(bp)
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"setBreakpoints failed: {exc}"]}

    session.ensure_disconnected_if_ephemeral()
    notes = []
    if not bp.verified:
        notes.append("Breakpoint not yet verified by debugpy. It may resolve when the module is loaded.")
    remote_file = session.to_remote_path(file)
    if remote_file != file:
        notes.append(f"Path mapped to remote: {remote_file}")
    return {
        "ok": True, "id": bp.internal_id, "file": file, "line": line,
        "verified": bp.verified, "condition": condition, "notes": notes,
    }


@mcp.tool()
def debugpy_remove_breakpoint(
    breakpoint_id: str,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Remove a breakpoint by its ID (returned from debugpy_set_breakpoint)."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    bp = next((b for b in session.breakpoints if b.internal_id == breakpoint_id), None)
    if bp is None:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"No breakpoint with id '{breakpoint_id}'."]}

    affected_file = bp.file
    session.breakpoints.remove(bp)

    try:
        _send_breakpoints_for_file(session, affected_file)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"Failed to update breakpoints after removal: {exc}"]}

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "notes": [f"Breakpoint {breakpoint_id} removed."]}


@mcp.tool()
def debugpy_list_breakpoints(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """List all registered breakpoints for this session."""
    try:
        session = _get_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}
    return {
        "ok": True,
        "breakpoints": [bp.to_dict() for bp in session.breakpoints],
        "notes": [f"{len(session.breakpoints)} breakpoint(s) registered."],
    }
```

- [ ] **Step 2: Verify syntax**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "from debugpy_mcp.server import debugpy_set_breakpoint; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/server.py && git commit -m "feat: add breakpoint MCP tools (set/remove/list)"
```

---

### Task 9: Inspection tools

**Files:**
- Modify: `src/debugpy_mcp/server.py`

- [ ] **Step 1: Add inspection tools**

Append to `server.py` (before `main()`):

```python
# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def debugpy_threads(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """List active threads and stack frames. Stopped threads include full stack traces."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    try:
        threads_resp = session._request("threads")
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP threads error: {exc}"]}

    threads_out = []
    for t in threads_resp.get("body", {}).get("threads", []):
        tid = t["id"]
        entry: dict[str, Any] = {"id": tid, "name": t.get("name", ""), "frames": []}
        # Attempt stackTrace; will fail for running threads — that's fine
        try:
            st_resp = session._request("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 20}, timeout=5.0)
            for frame in st_resp.get("body", {}).get("stackFrames", []):
                src = frame.get("source", {})
                remote_path = src.get("path", "")
                entry["frames"].append({
                    "id": frame["id"],
                    "name": frame.get("name", ""),
                    "file": session.to_local_path(remote_path),
                    "line": frame.get("line"),
                })
        except Exception:
            entry["frames"] = []
        threads_out.append(entry)

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "threads": threads_out, "notes": []}


@mcp.tool()
def debugpy_variables(
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    frame_id: int | None = None,
    scope: str = "locals",
) -> dict[str, Any]:
    """Inspect variables in a stopped frame.

    scope: 'locals' or 'globals' (case-insensitive match against DAP scope names).
    frame_id defaults to the current stopped frame.
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    fid = frame_id
    if fid is None:
        with session._state_lock:
            fid = session.stopped_frame_id
    if fid is None:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": ["No stopped frame. Pause the process or hit a breakpoint first."]}

    try:
        scopes_resp = session._request("scopes", {"frameId": fid})
        scopes = scopes_resp.get("body", {}).get("scopes", [])
        matched = next(
            (s for s in scopes if s.get("name", "").lower() == scope.lower()),
            None,
        )
        if matched is None:
            available = [s.get("name") for s in scopes]
            session.ensure_disconnected_if_ephemeral()
            return {"ok": False, "notes": [f"Scope '{scope}' not found. Available: {available}"]}

        vars_resp = session._request("variables", {"variablesReference": matched["variablesReference"]})
        variables = [
            {"name": v["name"], "value": v.get("value", ""), "type": v.get("type", "")}
            for v in vars_resp.get("body", {}).get("variables", [])
        ]
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}", "Frame ID may be stale — call debugpy_threads for fresh IDs."]}

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "frame_id": fid, "scope": scope, "variables": variables, "notes": []}


@mcp.tool()
def debugpy_evaluate(
    expression: str,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    frame_id: int | None = None,
    context: str = "watch",
) -> dict[str, Any]:
    """Evaluate a watch expression or REPL snippet.

    context: 'watch' (read-only style), 'repl' (can have side effects), or 'hover'.
    frame_id defaults to the current stopped frame.
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    fid = frame_id
    if fid is None:
        with session._state_lock:
            fid = session.stopped_frame_id

    args: dict[str, Any] = {"expression": expression, "context": context}
    if fid is not None:
        args["frameId"] = fid

    try:
        resp = session._request("evaluate", args)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}", "Frame ID may be stale — call debugpy_threads."]}

    session.ensure_disconnected_if_ephemeral()
    if not resp.get("success"):
        return {
            "ok": False,
            "expression": expression,
            "notes": [resp.get("message", "Evaluation failed."), "Ensure the process is stopped at a breakpoint."],
        }
    body = resp.get("body", {})
    return {
        "ok": True,
        "expression": expression,
        "result": body.get("result"),
        "type": body.get("type"),
        "notes": [],
    }
```

- [ ] **Step 2: Verify syntax and full server import**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "from debugpy_mcp.server import debugpy_evaluate, debugpy_variables, debugpy_threads; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add src/debugpy_mcp/server.py && git commit -m "feat: add inspection MCP tools (threads/variables/evaluate)"
```

---

## Chunk 3: README and Final Polish

### Task 10: Update README with all new sections

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read current README**

Read `README.md` to understand the current structure before editing.

- [ ] **Step 2: Append new sections to README**

Append the following content to the end of `README.md`:

```markdown
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
```

- [ ] **Step 3: Verify README renders (no broken markdown)**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "
with open('README.md') as f:
    content = f.read()
assert '## DAP Session Tools' in content
assert '## Path Mapping Setup' in content
assert '## Multi-Container Debugging' in content
assert '## Thread Worker Setups' in content
assert '## Future Architecture Options' in content
assert 'debugpy_connect' in content
assert 'TCP' in content
print('README sections verified ok')
"
```
Expected: `README sections verified ok`

- [ ] **Step 4: Commit**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add README.md && git commit -m "docs: add DAP session tools, path mapping, multi-container, and future options to README"
```

---

### Task 11: Final integration verification

- [ ] **Step 1: Verify the complete server imports cleanly**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && python -c "
import debugpy_mcp.server as s
tools = [name for name in dir(s) if name.startswith('debugpy_')]
print('Tools found:', sorted(tools))
"
```
Expected output includes all new tools:
```
Tools found: ['debugpy_attach', 'debugpy_autodiscover_target', 'debugpy_breakpoint_plan',
 'debugpy_connect', 'debugpy_context', 'debugpy_continue', 'debugpy_debugpy_logs',
 'debugpy_evaluate', 'debugpy_list_breakpoints', 'debugpy_list_containers', 'debugpy_logs',
 'debugpy_pause', 'debugpy_remove_breakpoint', 'debugpy_session_start', 'debugpy_session_status',
 'debugpy_session_stop', 'debugpy_set_breakpoint', 'debugpy_status', 'debugpy_step_in',
 'debugpy_step_out', 'debugpy_step_over', 'debugpy_threads', 'debugpy_variables']
```

- [ ] **Step 2: Commit final state**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git add -A && git status
# should show clean or only committed changes
```

- [ ] **Step 3: Tag the completion**

```bash
cd "/Users/william/r&d/debugpy-mcp-enhanced" && git log --oneline -10
```
Review the commit log to confirm all tasks landed cleanly.
