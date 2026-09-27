"""Local PTY bridge used by the HSD authoring workspace."""

import asyncio
import fcntl
import ipaddress
import json
import os
import pty
import signal
import shutil
import struct
import subprocess
import termios
from pathlib import Path
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect


def _terminal_enabled() -> bool:
    return os.environ.get("HSD_TERMINAL_ENABLED", "1").lower() not in {
        "0", "false", "no", "off",
    }


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return True
    return urlparse(origin).netloc == host


def _local_client(websocket: WebSocket) -> bool:
    if os.environ.get("HSD_TERMINAL_ALLOW_REMOTE", "0").lower() in {
        "1", "true", "yes", "on",
    }:
        return True
    if websocket.client is None:
        return False
    try:
        return ipaddress.ip_address(websocket.client.host).is_loopback
    except ValueError:
        return False


def _working_directory(raw: str | None) -> Path:
    candidate = Path(raw or os.path.expanduser("~")).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"Working directory does not exist: {candidate}")
    return candidate


def _resize(master_fd: int, rows: int, cols: int) -> None:
    rows = min(max(rows, 2), 300)
    cols = min(max(cols, 10), 500)
    fcntl.ioctl(
        master_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, cols, 0, 0),
    )


async def _pump_output(master_fd: int, websocket: WebSocket) -> None:
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time() + 0.5
    while True:
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            if loop.time() >= next_heartbeat:
                try:
                    # A quiet shell produces no output, so without a harmless
                    # empty frame a graceful browser close can go unnoticed by
                    # some ASGI servers until the PTY writes again.
                    await websocket.send_bytes(b"")
                except (OSError, RuntimeError, WebSocketDisconnect):
                    return
                next_heartbeat = loop.time() + 0.5
            await asyncio.sleep(0.01)
            continue
        except OSError:
            return
        if not data:
            return
        try:
            await websocket.send_bytes(data)
        except (OSError, RuntimeError, WebSocketDisconnect):
            return


async def _pump_input(master_fd: int, websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            os.write(master_fd, message["bytes"])
            continue
        raw = message.get("text")
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            os.write(master_fd, raw.encode())
            continue
        if payload.get("type") == "input":
            os.write(master_fd, str(payload.get("data", "")).encode())
        elif payload.get("type") == "resize":
            _resize(
                master_fd,
                int(payload.get("rows", 30)),
                int(payload.get("cols", 120)),
            )


async def terminal_websocket(
    websocket: WebSocket,
    *,
    purpose: str,
    provider: str,
    model: str,
    task_id: str = "",
    task_slug: str = "",
    herdr_session: str = "",
) -> None:
    """Bridge one same-origin WebSocket connection to one disposable shell PTY."""
    if not _terminal_enabled() or not _same_origin(websocket) or not _local_client(websocket):
        await websocket.close(code=1008)
        return

    try:
        cwd = _working_directory(websocket.query_params.get("cwd"))
    except ValueError:
        await websocket.close(code=1008)
        return

    shell_name = os.environ.get("HSD_TERMINAL_SHELL") or os.environ.get("SHELL") or "/usr/bin/zsh"
    shell = shutil.which(shell_name)
    if not shell or not os.access(shell, os.X_OK):
        await websocket.close(code=1011)
        return

    await websocket.accept()
    master_fd: int | None = None
    slave_fd: int | None = None
    process: subprocess.Popen | None = None
    tasks: list[asyncio.Task] = []
    try:
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        _resize(master_fd, 30, 120)
        env = os.environ.copy()
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "HSD_ACTIVITY": purpose,
            "HSD_PROVIDER": provider,
            "HSD_MODEL": model,
            "HSD_TASK_ID": task_id,
            "HSD_TASK_SLUG": task_slug,
            "HSD_HERDR_SESSION": herdr_session,
        })
        process = subprocess.Popen(
            [shell, "-l"],
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None
        banner = (
            f"\r\nHSD terminal · {purpose} · "
            f"{provider or 'provider unset'}/{model or 'model unset'} · {cwd}\r\n"
        )
        os.write(master_fd, banner.encode())
        tasks = [
            asyncio.create_task(_pump_output(master_fd, websocket)),
            asyncio.create_task(_pump_input(master_fd, websocket)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, ValueError, WebSocketDisconnect):
        pass
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.to_thread(process.wait, 2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.to_thread(process.wait, 2)
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
