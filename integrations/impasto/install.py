#!/usr/bin/env python3
"""Add the Archdrop module to an impasto (quickshell) config, or take it out again.

    install.py                       install into ~/.config/quickshell
    install.py --config DIR          install into another copy of the config
    install.py --uninstall           remove what this added
    install.py --dry-run             say what would change, change nothing

Two files are new (a service and a module). Five existing ones get small,
exact insertions: the catalogue, the module map, the type registry and the
island's resting activities. Every insertion is found by an anchor line and is
checked before anything is written, so an impasto that has moved on makes this
stop with a message rather than half-install. The originals are copied to
~/.local/state/archdrop/impasto-backup first.

After installing, add the module to the bar in Settings -> Bar (it is listed as
"Archdrop"), or reload the shell if it does not pick the files up by itself.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = HERE / "files"
NEW_FILES = ["services/DropService.qml", "bar/modules/DropModule.qml"]


@dataclass(frozen=True)
class Insertion:
    file: str
    anchor: str          # an existing line, matched after stripping
    text: str            # inserted after the anchor line (or before, if ``before``)
    before: bool = False


INSERTIONS = [
    Insertion("qmldir",
              "singleton BluetoothService   1.0 services/BluetoothService.qml",
              "singleton DropService        1.0 services/DropService.qml\n"),
    Insertion("qmldir",
              "BluetoothModule  1.0 bar/modules/BluetoothModule.qml",
              "DropModule       1.0 bar/modules/DropModule.qml\n"),

    Insertion("services/ModuleService.qml",
              '{ id: "bluetooth",     name: "Bluetooth",     bar: true,  width: 356, height: 132 },',
              '        { id: "drop",          name: "Archdrop",      bar: true,  desk: false, width: 380, height: 430 },\n'),
    Insertion("services/ModuleService.qml",
              'return BluetoothService.icon',
              '        case "drop":\n            return DropService.glyph\n'),
    Insertion("services/ModuleService.qml",
              'return BluetoothService.summary',
              '        case "drop":\n            return DropService.summary\n'),
    Insertion("services/ModuleService.qml",
              'case "timer":\n            return TimerService.running ? TimerService.tint : Theme.text',
              '        case "drop":\n            return DropService.tint\n', before=True),
    Insertion("services/ModuleService.qml",
              'return BluetoothService.available',
              '        case "drop":\n            // A placed piece always shows; "Offline" says the daemon is down.\n'
              '            return true\n'),
    Insertion("services/ModuleService.qml",
              'list.push("recorder")',
              '        if (DropService.busy)\n            list.push("drop")\n'),

    Insertion("bar/modules/Module.qml",
              "bluetooth: bluetoothModule,",
              "        drop: dropModule,\n"),
    Insertion("bar/modules/Module.qml",
              "Component { id: bluetoothModule;  BluetoothModule {} }",
              "    Component { id: dropModule;       DropModule {} }\n"),

    Insertion("bar/island/IslandRest.qml",
              "recorder: recorderMark, timer: timerMark, media: mediaMark",
              "            drop: dropMark,\n", before=True),
    Insertion("bar/island/IslandRest.qml",
              "recorder: recorderFigure, timer: timerFigure, media: mediaFigure",
              "            drop: dropFigure,\n", before=True),
    Insertion("bar/island/IslandRest.qml",
              "// ── COUNTDOWN ───────────────────────────────────────────────────────",
              '''        // ── DROP ────────────────────────────────────────────────────────────
        //
        // An offer waiting for an answer, or a transfer under way.
        Component {
            id: dropMark

            Text {
                text: DropService.glyph
                font.family: Theme.fontMono
                font.pixelSize: 13
                color: DropService.tint
            }
        }

        Component {
            id: dropFigure

            Text {
                text: DropService.summary
                font.family: Theme.fontMono
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.DemiBold
                color: DropService.tint
            }
        }

''', before=True),
]


# Insertions made by earlier versions, replaced on install and removed on
# uninstall.
LEGACY = {
    "services/ModuleService.qml": [
        '        { id: "drop",          name: "Drop",          bar: true,  desk: false, width: 380, height: 430 },\n',
    ],
}


def strip_legacy(rel: str, text: str) -> str:
    for old in LEGACY.get(rel, []):
        text = text.replace(old, "")
    return text


class Mismatch(Exception):
    pass


def find_line(lines: list[str], anchor: str) -> int:
    """Index of the line where the (possibly multi-line) anchor starts."""
    parts = [a.strip() for a in anchor.split("\n")]
    for i in range(len(lines) - len(parts) + 1):
        if all(parts[k] in lines[i + k] for k in range(len(parts))):
            return i
    raise Mismatch(anchor)


def plan_install(root: Path) -> dict[Path, str]:
    """New text for every file that must change; raises Mismatch before any write."""
    out: dict[Path, str] = {}
    for rel in {i.file for i in INSERTIONS}:
        path = root / rel
        if not path.exists():
            raise Mismatch(f"{rel} is missing")
        lines = strip_legacy(rel, path.read_text()).splitlines(keepends=True)
        for ins in (i for i in INSERTIONS if i.file == rel):
            body = "".join(lines)
            if ins.text in body:
                continue
            at = find_line(lines, ins.anchor)
            n = ins.anchor.count("\n") + 1
            position = at if ins.before else at + n
            lines[position:position] = [ins.text]
        out[path] = "".join(lines)
    return out


def plan_uninstall(root: Path) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for rel in {i.file for i in INSERTIONS}:
        path = root / rel
        if not path.exists():
            continue
        text = strip_legacy(rel, path.read_text())
        for ins in (i for i in INSERTIONS if i.file == rel):
            text = text.replace(ins.text, "")
        out[path] = text
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="~/.config/quickshell", type=Path)
    ap.add_argument("--archdrop", help="absolute path of the archdrop command (default: found on PATH)")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = args.config.expanduser().resolve()
    if not (root / "shell.qml").exists() or not (root / "services/ModuleService.qml").exists():
        print(f"{root} does not look like an impasto config (no shell.qml or ModuleService.qml).",
              file=sys.stderr)
        return 2

    if args.uninstall:
        changes = plan_uninstall(root)
        removals = [root / f for f in NEW_FILES if (root / f).exists()]
    else:
        binary = args.archdrop or shutil.which("archdrop") or str(HERE.parent.parent / "bin/archdrop")
        binary = str(Path(binary).expanduser().resolve())
        if not Path(binary).exists():
            print(f"archdrop not found at {binary}; pass --archdrop", file=sys.stderr)
            return 2
        try:
            changes = plan_install(root)
        except Mismatch as error:
            print("This impasto no longer matches what the installer expects, so nothing was "
                  f"changed.\nLooking for: {error}", file=sys.stderr)
            return 3
        removals = []

    changed = {p: t for p, t in changes.items() if p.read_text() != t}
    for p in changed:
        print(("would change " if args.dry_run else "changing ") + str(p.relative_to(root)))
    if args.uninstall:
        for p in removals:
            print(("would remove " if args.dry_run else "removing ") + str(p.relative_to(root)))
    else:
        for f in NEW_FILES:
            print(("would write " if args.dry_run else "writing ") + f)
    if args.dry_run:
        return 0

    if changed:
        backup = Path.home() / ".local/state/archdrop/impasto-backup" / time.strftime("%Y%m%d-%H%M%S")
        for p in changed:
            dest = backup / p.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        print(f"originals saved in {backup}")
        for p, t in changed.items():
            p.write_text(t)
    if args.uninstall:
        for p in removals:
            p.unlink()
    else:
        for f in NEW_FILES:
            target = root / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((FILES / f).read_text().replace("@ARCHDROP_BIN@", binary))
        print("done. Add Archdrop to the bar in Settings -> Bar, or reload the shell.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
