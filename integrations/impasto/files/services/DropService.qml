// ╭──────────────────────────────────────────────────────────────────────────╮
// │                                                                          │
// │   D R O P   S E R V I C E                                                │
// │   Quick Share · state of the archdrop daemon                            │
// │                                                                          │
// │   github.com/andreumassanet/impasto  (module added by archdrop)          │
// │                                                                          │
// ╰──────────────────────────────────────────────────────────────────────────╯

pragma Singleton

import QtQml
import QtQuick
import Quickshell
import Quickshell.Io

import "../theme"

// A mirror of the daemon's state. Nothing is polled: `archdrop watch` prints the
// whole state as one JSON line whenever it changes, and this reads those lines.
// Commands go the other way as short `archdrop` calls, and their effect comes
// back through the same stream.
//
// Nearby devices are found only while a detail asks for them (`discover`), so
// an idle shell does not keep the network browsing.
Singleton {
    id: root

    readonly property string binary: "@ARCHDROP_BIN@"

    // The daemon answered at least once, and its stream is still open.
    property bool ready: false

    property string name: ""
    property bool receiving: false
    property real until: 0
    property bool autoAccept: false
    property var backends: ({})
    property var peers: []
    property var transfers: []
    property int pending: 0
    property int active: 0

    // A new offer opens the island once, so it cannot be missed.
    property bool openOnOffer: true

    readonly property bool busy: root.pending > 0 || root.active > 0

    readonly property var offers: root.transfers.filter(t => t.state === "pending")
    readonly property var running: root.transfers.filter(t =>
        t.state === "active" || t.state === "waiting" || t.state === "connecting")
    readonly property var recent: root.transfers.filter(t =>
        t.state === "done" || t.state === "failed" || t.state === "rejected" || t.state === "cancelled")

    // The one transfer the bar and the resting island speak for.
    readonly property var lead: root.offers.length > 0 ? root.offers[0]
        : (root.running.length > 0 ? root.running[0] : null)

    readonly property real progress: {
        const t = root.lead
        return t && t.total > 0 ? Math.min(1, t.done / t.total) : 0
    }

    readonly property string glyph: root.ready ? "" : "󰀦"

    readonly property string minutesLeft: {
        if (!root.receiving || root.until <= 0)
            return ""
        const minutes = Math.max(0, Math.ceil((root.until - root.clock.date.getTime() / 1000) / 60))
        return `${minutes} min`
    }

    readonly property string summary: {
        if (!root.ready)
            return "Offline"
        if (root.offers.length > 0)
            return "Accept?"
        if (root.running.length > 0)
            return root.lead.total > 0 ? `${Math.round(root.progress * 100)}%` : "…"
        return root.receiving ? "Visible" : "Off"
    }

    // Warnings borrow the fixed indicator hues, like the battery does.
    readonly property color tint: root.offers.length > 0 ? Theme.indicatorWarn
        : (root.running.length > 0 ? Theme.indicatorTimer : Theme.text)

    readonly property SystemClock clock: SystemClock {
        precision: SystemClock.Minutes
        enabled: root.receiving && root.until > 0
    }

    // ── COMMANDS ────────────────────────────────────────────────────────────

    function run(args: var): void {
        Quickshell.execDetached([root.binary].concat(args))
    }

    function toggle(): void {
        root.run(["toggle"])
    }

    function setReceiving(on: bool): void {
        root.run(on ? ["on"] : ["off"])
    }

    function accept(id: string): void {
        root.run(["accept", id])
    }

    function reject(id: string): void {
        root.run(["reject", id])
    }

    function cancel(id: string): void {
        root.run(["cancel", id])
    }

    // Opens the desktop's file chooser and sends what is picked. The island
    // closes first: it holds the keyboard and sits above the dialog.
    function sendFiles(peerId: string): void {
        ModuleService.close()
        root.run(["send", "--pick", "--detach", "--notify", "--to", peerId])
    }

    // Sends what is on the clipboard: copied files, an image, or text.
    function sendClipboard(peerId: string): void {
        root.run(["send", "--clipboard", "--detach", "--notify", "--to", peerId])
    }

    function startDaemon(): void {
        Quickshell.execDetached(["systemctl", "--user", "start", "archdropd.service"])
    }

    // ── DISCOVERY ───────────────────────────────────────────────────────────

    property int discoverers: 0

    function discover(on: bool): void {
        root.discoverers = Math.max(0, root.discoverers + (on ? 1 : -1))
    }

    // A second stream whose only job is to hold discovery open; the peers
    // arrive on the first.
    readonly property Process discovery: Process {
        command: [root.binary, "watch", "--peers"]
        running: root.discoverers > 0 && root.ready
        stdout: SplitParser { onRead: line => {} }
    }

    // ── THE STREAM ──────────────────────────────────────────────────────────

    property var seenOffers: ({})

    function apply(line: string): void {
        let state = null
        try {
            state = JSON.parse(line)
        } catch (error) {
            console.warn("Cannot parse the archdrop state:", error)
            return
        }
        root.ready = true
        root.name = state.name ?? ""
        root.receiving = state.receiving === true
        root.until = state.until ?? 0
        root.autoAccept = state.auto_accept === true
        root.backends = state.backends ?? ({})
        root.peers = state.peers ?? []
        root.transfers = state.transfers ?? []
        root.pending = state.pending ?? 0
        root.active = state.active ?? 0

        for (const offer of root.offers) {
            if (root.seenOffers[offer.id])
                continue
            root.seenOffers[offer.id] = true
            if (root.openOnOffer && ModuleService.openId !== "drop")
                ModuleService.activate("drop")
        }
    }

    readonly property Process watcher: Process {
        command: [root.binary, "watch"]
        running: true
        stdout: SplitParser { onRead: line => root.apply(line) }
        onExited: {
            root.ready = false
            root.peers = []
            retry.restart()
        }
    }

    // The daemon may not be up yet, or may restart; either way this finds it.
    readonly property Timer retry: Timer {
        interval: 4000
        onTriggered: root.watcher.running = true
    }
}
