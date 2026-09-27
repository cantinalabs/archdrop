"""The ``archdrop`` command: a thin client of the daemon's socket."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

from . import __version__, config

GLYPH = ""   # Font Awesome share-alt, present in every Nerd Font


class DaemonDown(Exception):
    pass


def connect() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(str(config.socket_path()))
    except OSError as error:
        sock.close()
        raise DaemonDown from error
    return sock


def request(**message: Any) -> dict:
    sock = connect()
    with sock:
        sock.sendall((json.dumps(message) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def stream(**message: Any) -> Iterator[dict]:
    sock = connect()
    with sock, sock.makefile("rb") as reader:
        sock.sendall((json.dumps(message) + "\n").encode())
        for line in reader:
            yield json.loads(line)


def die(text: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    global _last_error
    _last_error = text
    print(f"archdrop: {text}", file=sys.stderr)
    raise SystemExit(code)


def parse_minutes(text: str) -> int | None:
    """``10``, ``10m``, ``1h``, ``90s`` -> minutes; ``forever`` -> None."""
    if text in ("forever", "always", "0"):
        return None
    match = re.fullmatch(r"(\d+)\s*([smh]?)", text)
    if not match:
        die(f"cannot read {text!r} as a duration (try 10m, 1h or forever)")
    value, unit = int(match[1]), match[2] or "m"
    return max(1, math.ceil({"s": value / 60, "m": value, "h": value * 60}[unit]))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def need_state() -> dict:
    try:
        return request(cmd="status")["state"]
    except DaemonDown:
        die("archdropd is not running (systemctl --user start archdropd)", 3)


# ── status text ────────────────────────────────────────────────────────────

def describe(state: dict) -> str:
    lines = []
    if state["receiving"]:
        until = state["until"]
        left = f", {int((until - time.time()) // 60) + 1} min left" if until else ""
        lines.append(f"Visible as \"{state['name']}\"{left}")
    else:
        lines.append("Not receiving")
    for name, backend in state["backends"].items():
        if backend["available"]:
            lines.append(f"  {name}: {'receiving' if backend['receiving'] else 'idle'}")
        else:
            lines.append(f"  {name}: unavailable ({backend['note']})")
    for t in state["transfers"]:
        if t["state"] in ("pending", "active", "waiting", "connecting"):
            lines.append(f"  {t['id']} {t['direction']} {t['state']} {t['peer']}: "
                         f"{', '.join(t['files']) or t['kind']}")
    return "\n".join(lines)


def waybar_payload(state: dict | None) -> dict:
    if state is None:
        return {"text": GLYPH, "alt": "offline", "class": ["offline"],
                "tooltip": "archdropd is not running"}
    active = [t for t in state["transfers"] if t["state"] == "active"]
    pending = [t for t in state["transfers"] if t["state"] == "pending"]
    classes = []
    if pending:
        mode, text = "pending", f"{GLYPH} {pending[0]['pin']}"
        classes.append("pending")
    elif active:
        t = active[0]
        pct = int(100 * t["done"] / t["total"]) if t["total"] else 0
        mode, text = "transferring", f"{GLYPH} {pct}%"
        classes.append("transferring")
    elif state["receiving"]:
        mode, text = "visible", GLYPH
        classes.append("visible")
    else:
        mode, text = "off", GLYPH
        classes.append("off")
    tip = [describe(state)]
    for t in pending:
        tip.append(f"{t['peer']} wants to send {', '.join(t['files']) or t['kind']} (code {t['pin']})")
    for t in active:
        pct = int(100 * t["done"] / t["total"]) if t["total"] else 0
        tip.append(f"{t['peer']}: {pct}% of {human_bytes(t['total'])}")
    return {"text": text, "alt": mode, "class": classes, "tooltip": "\n".join(tip),
            "percentage": 100 if state["receiving"] else 0}


# ── commands ───────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    state = need_state()
    print(json.dumps(state, indent=2) if args.json else describe(state))
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    minutes = parse_minutes(args.duration) if args.duration else None
    kwargs = {"minutes": minutes} if args.duration else {}
    try:
        reply = request(cmd="on", **kwargs)
    except DaemonDown:
        die("archdropd is not running (systemctl --user start archdropd)", 3)
    if reply.get("error"):
        print(f"archdrop: {reply['error']}", file=sys.stderr)
    print(describe(reply["state"]))
    return 0 if reply["ok"] else 1


def cmd_off(args: argparse.Namespace) -> int:
    print(describe(request(cmd="off")["state"]))
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    print(describe(request(cmd="toggle")["state"]))
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    try:
        reply = request(cmd="peers", seconds=args.scan)
    except DaemonDown:
        die("archdropd is not running (systemctl --user start archdropd)", 3)
    if args.json:
        print(json.dumps(reply["peers"]))
    elif not reply["peers"]:
        print("No devices found. On Android, open Quick Share and set it to receive "
              "from Everyone, on the same network.")
    else:
        for p in reply["peers"]:
            print(f"{p['id']:<40} {p['backend']:<10} {p['kind']:<8} {p['name']}")
    return 0


def clipboard_items() -> tuple[list[str], str | None]:
    """Files or text on the Wayland clipboard: ``(paths, text)``."""
    if not shutil.which("wl-paste"):
        die("reading the clipboard needs wl-clipboard")
    types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True).stdout.split()
    if "text/uri-list" in types:
        raw = subprocess.run(["wl-paste", "--type", "text/uri-list"], capture_output=True,
                             text=True).stdout
        paths = []
        for line in raw.splitlines():
            if line.startswith("file://"):
                paths.append(urllib.parse.unquote(urllib.parse.urlparse(line.strip()).path))
        if paths:
            return paths, None
    for kind in types:
        if kind.startswith("image/"):
            ext = kind.split("/")[1].split("+")[0]
            target = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"archdrop-clip-{int(time.time())}.{ext}"
            data = subprocess.run(["wl-paste", "--type", kind], capture_output=True).stdout
            target.write_bytes(data)
            return [str(target)], None
    if any(t.startswith("text/plain") or t in ("UTF8_STRING", "STRING") for t in types):
        text = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True).stdout
        if text.strip():
            return [], text
    return [], None


LAST_FOLDER = config.STATE_DIR / "last-send-folder"


def pick(peer: str | None) -> list[str] | None:
    """Files from the desktop's file chooser, starting where the last ones came from."""
    from . import dbus
    try:
        folder = LAST_FOLDER.read_text().strip()
    except OSError:
        folder = ""
    if not folder or not Path(folder).is_dir():
        folder = str(Path.home())
    title = "Send with Archdrop"
    try:
        paths = dbus.pick_files(title, folder)
    except (OSError, dbus.DBusError) as error:
        die(f"could not open a file chooser ({error}); is xdg-desktop-portal running?")
    if paths:
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            LAST_FOLDER.write_text(str(Path(paths[0]).parent))
        except OSError:
            pass
    return paths or None


def cmd_send(args: argparse.Namespace) -> int:
    try:
        return send_now(args)
    except SystemExit as stop:
        # Started from a bar there is no terminal to read an error in.
        if args.notify and stop.code not in (0, None) and shutil.which("notify-send"):
            from .notify import ICON
            subprocess.run(["notify-send", "--app-name=archdrop", "--icon", str(ICON), "Could not send",
                            _last_error or "see `archdrop send` in a terminal"], check=False)
        raise


_last_error = ""


def send_now(args: argparse.Namespace) -> int:
    paths = [str(Path(p).resolve()) for p in args.files]
    text = args.text
    if args.clipboard:
        paths, text = clipboard_items()
        if not paths and text is None:
            die("the clipboard holds no file or text to send")
    if args.pick:
        picked = pick(args.to)
        if picked is None:
            return 0                      # cancelled in the chooser
        paths = picked
    if not paths and text is None:
        die("give files to send, or --pick, --text or --clipboard")
    if not args.to:
        peers = request(cmd="peers", seconds=4)["peers"]
        if len(peers) == 1:
            args.to = peers[0]["id"]
        elif not peers:
            die("no devices found (set the phone to receive from Everyone, on this network)")
        else:
            die("several devices found; choose one with --to:\n  " +
                "\n  ".join(f"{p['name']}  ({p['id']})" for p in peers))
    try:
        reply = request(cmd="send", peer=args.to, paths=paths, text=text)
    except DaemonDown:
        die("archdropd is not running (systemctl --user start archdropd)", 3)
    if not reply["ok"]:
        die(reply["error"])
    if args.detach:
        print(reply["id"])
        return 0
    return follow(reply["id"])


def follow(transfer_id: str) -> int:
    last = ""
    for event in stream(cmd="watch"):
        transfer = next((t for t in event["state"]["transfers"] if t["id"] == transfer_id), None)
        if transfer is None:
            continue
        pct = int(100 * transfer["done"] / transfer["total"]) if transfer["total"] else 0
        line = f"{transfer['state']:<10} {transfer['peer']}  {pct}%"
        if transfer["pin"] and transfer["state"] in ("waiting", "active"):
            line += f"  (code {transfer['pin']} should match on the other screen)"
        if line != last:
            print(line, flush=True)
            last = line
        if transfer["state"] in ("done", "failed", "rejected", "cancelled"):
            if transfer["error"]:
                print(f"archdrop: {transfer['error']}", file=sys.stderr)
            return 0 if transfer["state"] == "done" else 1
    return 1


def cmd_answer(verdict: str):
    def run(args: argparse.Namespace) -> int:
        reply = request(cmd=verdict, id=args.id) if args.id else request(cmd=verdict)
        if not reply["ok"]:
            die(reply["error"])
        return 0
    return run


def cmd_cancel(args: argparse.Namespace) -> int:
    reply = request(cmd="cancel", id=args.id)
    if not reply["ok"]:
        die(reply["error"])
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    expected = config.EDITABLE.get(args.key)
    if expected is None:
        die(f"unknown setting {args.key!r}; choose from {', '.join(config.EDITABLE)}")
    value: Any = args.value
    if expected is bool:
        value = args.value.lower() in ("1", "true", "yes", "on")
    elif expected is int:
        value = int(args.value)
    reply = request(cmd="set", key=args.key, value=value)
    if not reply["ok"]:
        die(reply["error"])
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        for event in stream(cmd="watch", peers=args.peers):
            print(json.dumps(event["state"]), flush=True)
    except DaemonDown:
        die("archdropd is not running", 3)
    return 0


def cmd_waybar(args: argparse.Namespace) -> int:
    """Stream Waybar custom-module JSON, and reconnect if the daemon restarts."""
    try:
        while True:
            try:
                for event in stream(cmd="watch"):
                    print(json.dumps(waybar_payload(event["state"])), flush=True)
            except (DaemonDown, ConnectionError, ValueError):
                pass
            print(json.dumps(waybar_payload(None)), flush=True)
            time.sleep(2)
    except BrokenPipeError:
        return 0  # Waybar went away


def cmd_trace(args: argparse.Namespace) -> int:
    from . import trace
    return trace.run(args.seconds, args.all)


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    def line(good: bool | None, text: str) -> None:
        nonlocal ok
        ok = ok and good is not False
        print(f"  {'ok  ' if good else ('warn' if good is None else 'FAIL')}  {text}")

    print("archdrop doctor")
    for module, package in (("cryptography", "python-cryptography"), ("zeroconf", "python-zeroconf")):
        try:
            __import__(module)
            line(True, f"{module} is installed")
        except ImportError:
            line(False, f"{module} is missing: pacman -S {package}")
    try:
        state = request(cmd="status")["state"]
        line(True, f"daemon {state['daemon']} is running")
        for name, b in state["backends"].items():
            line(True if b["available"] else None,
                 f"{name}: " + ("available" if b["available"] else b["note"]))
    except DaemonDown:
        line(False, "the daemon is not running: systemctl --user enable --now archdropd")
    for tool, why in (("notify-send", "notifications"), ("wl-copy", "copying received text"),
                      ("wl-paste", "sending the clipboard")):
        line(True if shutil.which(tool) else None, f"{tool} " + ("found" if shutil.which(tool)
                                                                else f"not found ({why} will not work)"))
    nets = [p.parent.name for p in Path("/sys/class/net").glob("*/wireless")]
    line(True if nets else None, "Wi-Fi: " + (", ".join(nets) if nets else
         "none (fine: Quick Share here works over Ethernet)"))
    has_bt = Path("/sys/class/bluetooth").exists() and any(Path("/sys/class/bluetooth").iterdir())
    line(True if has_bt else None, "Bluetooth: " + ("present" if has_bt else
         "none (Quick Share here uses the network only)"))
    fw = shutil.which("ufw")
    if fw:
        print("  note  ufw found: Quick Share receives on a TCP port and finds peers with mDNS "
              "(UDP 5353). Set `port = 47893` in config.toml and allow that port and 5353/udp.")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archdrop", description=(
        "Android Quick Share from the command line and your bar."))
    parser.add_argument("--version", action="version", version=f"archdrop {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="what the service is doing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("on", help="become visible to nearby devices and receive")
    p.add_argument("duration", nargs="?", help="10m, 1h, forever (default: config)")
    p.set_defaults(fn=cmd_on)
    sub.add_parser("off", help="stop receiving").set_defaults(fn=cmd_off)
    sub.add_parser("toggle", help="on if off, off if on").set_defaults(fn=cmd_toggle)

    p = sub.add_parser("peers", help="devices that can receive from you right now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--scan", type=float, default=4, help="seconds to listen (default 4)")
    p.set_defaults(fn=cmd_peers)

    p = sub.add_parser("send", help="send files, text or the clipboard")
    p.add_argument("files", nargs="*")
    p.add_argument("--to", help="device name or id (see `archdrop peers`)")
    p.add_argument("--text", help="send this text or link instead of files")
    p.add_argument("--clipboard", action="store_true", help="send what is on the clipboard")
    p.add_argument("--pick", action="store_true", help="choose the files in a file chooser")
    p.add_argument("--detach", action="store_true", help="return at once, print the transfer id")
    p.add_argument("--notify", action="store_true", help="report a failure as a notification")
    p.set_defaults(fn=cmd_send)

    for verdict in ("accept", "reject"):
        p = sub.add_parser(verdict, help=f"{verdict} the incoming transfer")
        p.add_argument("id", nargs="?")
        p.set_defaults(fn=cmd_answer(verdict))
    p = sub.add_parser("cancel", help="cancel a transfer")
    p.add_argument("id")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("set", help="change a setting: " + ", ".join(config.EDITABLE))
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("watch", help="print the state as JSON lines, on every change")
    p.add_argument("--peers", action="store_true", help="also keep discovering devices")
    p.set_defaults(fn=cmd_watch)
    sub.add_parser("waybar", help="Waybar custom module: JSON lines on stdout").set_defaults(fn=cmd_waybar)
    p = sub.add_parser("trace", help="listen for what a phone asks for on the network")
    p.add_argument("seconds", nargs="?", type=float, default=30)
    p.add_argument("--all", action="store_true", help="also show unrelated mDNS traffic")
    p.set_defaults(fn=cmd_trace)
    sub.add_parser("doctor", help="check what is missing").set_defaults(fn=cmd_doctor)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(args.fn(args))
    except DaemonDown:
        die("archdropd is not running (systemctl --user start archdropd)", 3)
    except KeyboardInterrupt:
        raise SystemExit(130)
