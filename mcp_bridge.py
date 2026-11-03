"""Knowledge-RAG Singleton MCP Bridge.

Bridges standard MCP stdio JSON-RPC to the persistent background knowledge-rag daemon (SSE).
Ensures only ONE instance of the heavy embedding server ever runs on the host, preventing
RAM bloat, VRAM exhaustion (CUDA OOM), and database lock collisions across multiple clients
(Claude Desktop, Gemini Antigravity, IDEs).
"""

from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

import anyio
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server

SSE_URL = "http://127.0.0.1:8179/sse"
HEALTH_URL = "http://127.0.0.1:8179/health"


def ensure_server_running() -> None:
    """Verify background daemon is alive; auto-spawn detached daemon if not."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass

    # Start detached daemon in background
    base_dir = Path(r"Z:\knowledge-rag")
    python_exe = base_dir / "venv" / "Scripts" / "python.exe"
    cmd = [str(python_exe), "-m", "mcp_server.server"]

    # Detach process on Windows
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS

    subprocess.Popen(
        cmd,
        cwd=str(base_dir),
        creationflags=flags,
        close_fds=True,
    )

    # Wait up to 15 seconds for health endpoint
    for _ in range(30):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass


async def bridge_stdio_to_sse() -> None:
    """Forward messages between stdin/stdout and SSE endpoint."""
    ensure_server_running()
    async with stdio_server() as (stdin_r, stdout_w):
        async with sse_client(SSE_URL) as (sse_r, sse_w):

            async def forward_in():
                async for msg in stdin_r:
                    await sse_w.send(msg)

            async def forward_out():
                async for msg in sse_r:
                    await stdout_w.send(msg)

            async with anyio.create_task_group() as tg:
                tg.start_soon(forward_in)
                tg.start_soon(forward_out)


def main() -> None:
    anyio.run(bridge_stdio_to_sse)


if __name__ == "__main__":
    main()
