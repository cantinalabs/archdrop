"""Desktop notifications and the clipboard, by the tools already on the system."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

# Shipped with the package and passed by path, so it never depends on the icon
# theme: a name the theme lacks is drawn as a broken image by some daemons.
ICON = Path(__file__).resolve().parent / "icons" / "archdrop.png"


async def _quiet(*argv: str, stdin: bytes | None = None) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(stdin), 5)
    except (OSError, asyncio.TimeoutError):
        pass


async def notify(title: str, body: str = "", *, urgent: bool = False) -> None:
    if not shutil.which("notify-send"):
        return
    args = ["notify-send", "--app-name=archdrop", "--urgency", "critical" if urgent else "normal"]
    if ICON.exists():
        args += ["--icon", str(ICON), f"--hint=string:image-path:{ICON}"]
    await _quiet(*args, "--", title, body)


async def copy(text: str) -> bool:
    """Put text on the Wayland clipboard. False when there is no way to."""
    if not shutil.which("wl-copy"):
        return False
    await _quiet_copy(text)
    return True


async def _quiet_copy(text: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "wl-copy", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        # wl-copy stays alive serving the selection; do not wait for it.
    except OSError:
        pass
