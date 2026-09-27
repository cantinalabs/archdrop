// ╭──────────────────────────────────────────────────────────────────────────╮
// │                                                                          │
// │   D R O P   M O D U L E                                                  │
// │   Quick Share · offers, transfers, nearby devices                        │
// │                                                                          │
// │   github.com/andreumassanet/impasto  (module added by archdrop)          │
// │                                                                          │
// ╰──────────────────────────────────────────────────────────────────────────╯

import QtQuick
import QtQuick.Layouts
import Quickshell

import "../../theme"
import "../../services"
import "../../components"

// The chip shows the state in one word; the detail is where an offer is
// answered, a transfer watched, and a device chosen to send to. Choosing a
// device opens the desktop's file chooser; the clipboard button beside it sends
// whatever was copied (files from the file manager, an image, or text).
Item {
    id: root

    implicitWidth: holder.implicitWidth
    implicitHeight: holder.implicitHeight

    Component.onCompleted: DropService.discover(true)
    Component.onDestruction: DropService.discover(false)

    function size(bytes: real): string {
        const units = ["B", "KB", "MB", "GB"]
        let value = bytes
        let unit = 0
        while (value >= 1000 && unit < units.length - 1) {
            value /= 1000
            unit += 1
        }
        return `${unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
    }

    function kindGlyph(kind: string): string {
        return kind === "laptop" ? "󰟀" : "󰄜"
    }

    function offerText(t: var): string {
        if (t.kind !== "files")
            return t.kind === "url" ? "a link" : "a message"
        const first = t.files[0] ?? "a file"
        return t.files.length > 1 ? `${first} and ${t.files.length - 1} more` : first
    }

    Loader {
        id: holder
        anchors.fill: parent
        sourceComponent: detail
    }

    Component {
        id: detail

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            anchors.topMargin: 12
            anchors.bottomMargin: 12
            spacing: 10

            // ── HEADER ──────────────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: 13

                RingIndicator {
                    Layout.preferredWidth: 44
                    Layout.preferredHeight: 44
                    thickness: 2.5
                    progress: DropService.progress
                    trackColor: Theme.indicatorDim
                    fillColor: DropService.tint

                    Text {
                        anchors.centerIn: parent
                        text: DropService.glyph
                        font.family: Theme.fontMono
                        font.pixelSize: 18
                        color: DropService.receiving || DropService.busy
                            ? DropService.tint : Theme.textMuted
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Text {
                        Layout.fillWidth: true
                        text: "Archdrop"
                        elide: Text.ElideRight
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeMedium
                        font.weight: Font.DemiBold
                        color: Theme.text
                    }

                    Text {
                        Layout.fillWidth: true
                        text: {
                            if (!DropService.ready)
                                return "The service is not running"
                            if (!DropService.receiving)
                                return "Not receiving"
                            const left = DropService.minutesLeft
                            return left !== ""
                                ? `Visible as ${DropService.name} · ${left}`
                                : `Visible as ${DropService.name}`
                        }
                        elide: Text.ElideRight
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.textMuted
                    }
                }

                ToggleSwitch {
                    visible: DropService.ready
                    checked: DropService.receiving
                    onToggled: checked => DropService.setReceiving(checked)
                }

                PillButton {
                    visible: !DropService.ready
                    text: "Start"
                    onClicked: DropService.startDaemon()
                }
            }

            // ── OFFERS ──────────────────────────────────────────────────────
            Repeater {
                model: DropService.offers

                delegate: Rectangle {
                    id: offer

                    required property var modelData

                    Layout.fillWidth: true
                    implicitHeight: offerColumn.implicitHeight + 20
                    radius: Theme.radiusMedium
                    color: Theme.islandSurface
                    border.color: Theme.indicatorWarn
                    border.width: 1

                    ColumnLayout {
                        id: offerColumn
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1

                                Text {
                                    Layout.fillWidth: true
                                    text: `${offer.modelData.peer} wants to send`
                                    elide: Text.ElideRight
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.textMuted
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: root.offerText(offer.modelData)
                                    elide: Text.ElideMiddle
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeRegular
                                    font.weight: Font.DemiBold
                                    color: Theme.text
                                }

                                Text {
                                    Layout.fillWidth: true
                                    visible: offer.modelData.total > 0
                                    text: root.size(offer.modelData.total)
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeLabel
                                    color: Theme.textMuted
                                }
                            }

                            // The same four digits appear on the sender's
                            // screen; a match means nobody is in between.
                            ColumnLayout {
                                spacing: 0

                                Text {
                                    Layout.alignment: Qt.AlignHCenter
                                    text: "CODE"
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeLabel
                                    font.weight: Font.DemiBold
                                    font.letterSpacing: 0.6
                                    color: Theme.textMuted
                                }

                                Text {
                                    text: offer.modelData.pin
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.fontSizeLarge
                                    font.weight: Font.DemiBold
                                    color: Theme.indicatorWarn
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            PillButton {
                                Layout.fillWidth: true
                                text: "Accept"
                                active: true
                                onClicked: DropService.accept(offer.modelData.id)
                            }

                            PillButton {
                                Layout.fillWidth: true
                                text: "Decline"
                                onClicked: DropService.reject(offer.modelData.id)
                            }
                        }
                    }
                }
            }

            // ── TRANSFERS IN FLIGHT ─────────────────────────────────────────
            Repeater {
                model: DropService.running

                delegate: ColumnLayout {
                    id: flight

                    required property var modelData

                    Layout.fillWidth: true
                    spacing: 4

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Text {
                            Layout.fillWidth: true
                            text: `${flight.modelData.direction === "in" ? "From" : "To"} ${flight.modelData.peer}`
                                + (flight.modelData.state === "waiting" ? " · waiting for them to accept" : "")
                            elide: Text.ElideRight
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.text
                        }

                        Text {
                            visible: flight.modelData.total > 0 && flight.modelData.state === "active"
                            text: `${Math.round(100 * flight.modelData.done / flight.modelData.total)}%`
                            font.family: Theme.fontMono
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.textMuted
                        }

                        IconButton {
                            icon: "󰅖"
                            iconSize: 12
                            implicitWidth: 24
                            implicitHeight: 22
                            onClicked: DropService.cancel(flight.modelData.id)
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 4
                        radius: 2
                        color: Theme.indicatorDim

                        Rectangle {
                            width: flight.modelData.total > 0
                                ? parent.width * Math.min(1, flight.modelData.done / flight.modelData.total)
                                : 0
                            height: parent.height
                            radius: 2
                            color: Theme.indicatorTimer

                            Behavior on width { NumberAnimation { duration: Theme.durationFast } }
                        }
                    }
                }
            }

            // ── NEARBY DEVICES ──────────────────────────────────────────────
            Text {
                visible: DropService.ready
                text: "SEND TO"
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                font.weight: Font.DemiBold
                font.letterSpacing: 0.6
                color: Theme.textMuted
            }

            Text {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: DropService.ready && DropService.peers.length === 0
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                text: "No devices nearby yet.\nOn Android, open Quick Share and choose Everyone."
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.textMuted
            }

            ListView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: DropService.peers.length > 0
                clip: true
                spacing: 4
                boundsBehavior: Flickable.StopAtBounds
                model: DropService.peers

                delegate: Rectangle {
                    id: peer

                    required property var modelData

                    width: ListView.view.width
                    height: 44
                    radius: Theme.radiusMedium
                    color: peerMouse.containsMouse ? Theme.islandSurfaceHover : Theme.islandSurface
                    border.color: peerMouse.containsMouse ? Theme.accent : Theme.islandBorder
                    border.width: 1

                    Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                    // Above the row's click target: text lets clicks through to
                    // it, the clipboard button takes its own.
                    RowLayout {
                        z: 1
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 6
                        spacing: 10

                        Text {
                            text: root.kindGlyph(peer.modelData.kind)
                            font.family: Theme.fontMono
                            font.pixelSize: 15
                            color: peerMouse.containsMouse ? Theme.accent : Theme.textMuted
                        }

                        Text {
                            Layout.fillWidth: true
                            text: peer.modelData.name
                            elide: Text.ElideRight
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.text
                        }

                        Text {
                            text: "Choose files…"
                            visible: peerMouse.containsMouse
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            color: Theme.textMuted
                        }

                        IconButton {
                            icon: "󰅇"
                            iconSize: 13
                            implicitWidth: 28
                            implicitHeight: 26
                            onClicked: DropService.sendClipboard(peer.modelData.id)
                        }
                    }

                    MouseArea {
                        id: peerMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: DropService.sendFiles(peer.modelData.id)
                    }
                }
            }
        }
    }
}
