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
