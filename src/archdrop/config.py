"""Settings: defaults, then ``config.toml``, then what the CLI saved."""

from __future__ import annotations

import json
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _dir(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env) or os.path.expanduser(fallback))


CONFIG_DIR = _dir("XDG_CONFIG_HOME", "~/.config") / "archdrop"
STATE_DIR = _dir("XDG_STATE_HOME", "~/.local/state") / "archdrop"


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/archdrop-{os.getuid()}"
    return Path(runtime) / "archdrop.sock"


def default_download_dir() -> Path:
    try:
        for line in (Path.home() / ".config/user-dirs.dirs").read_text().splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                return Path(os.path.expandvars(value.replace("$HOME", str(Path.home()))))
    except OSError:
        pass
    return Path.home() / "Downloads"


# Settings the CLI may change, with the type each must have.
EDITABLE = {"name": str, "download_dir": str, "auto_accept": bool, "notify": bool,
            "default_minutes": int}


@dataclass
class Settings:
    name: str = ""
    download_dir: str = ""
    port: int = 0
    # Visible whenever the service runs, until switched off: a restart or a
    # login must not leave the computer silently missing from the phone.
    receive_on_start: bool = True
    default_minutes: int = 0
    auto_accept: bool = False
    notify: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def device_name(self) -> str:
        return self.name or socket.gethostname()

    @property
    def download_path(self) -> Path:
        return Path(os.path.expanduser(self.download_dir)) if self.download_dir \
            else default_download_dir()


def load() -> Settings:
    s = Settings()
    try:
        data = tomllib.loads((CONFIG_DIR / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    for key, value in data.items():
        if hasattr(s, key) and isinstance(value, type(getattr(s, key))):
            setattr(s, key, value)
    try:
        saved = json.loads((STATE_DIR / "settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    for key, value in saved.items():
        if key in EDITABLE and isinstance(value, EDITABLE[key]):
            setattr(s, key, value)
    return s


def save_override(key: str, value: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "settings.json"
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    saved[key] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=2))
    tmp.replace(path)
