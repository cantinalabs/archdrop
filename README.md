# archdrop

Android Quick Share for Arch, as a user service with a command line, a module for
Waybar and one for the [impasto](https://github.com/andreumassanet/impasto)
island.

```
   Android phone                                        your bar
  ┌───────────┐   Quick Share over the LAN            ┌──────────────┐  JSON lines   ┌─────────┐
  │           │ ◄────────────────────────────────────►│  archdropd   │ ─────────────►│ Waybar  │
  └───────────┘                                       │  (systemd    │               ├─────────┤
                                                      │   user unit) │ ─────────────►│ impasto │
                                                      └──────┬───────┘               └─────────┘
                                                        archdrop CLI
```

**Read this first**

* Quick Share is implemented here from the protocol up (see
  [docs/PROTOCOL.md](docs/PROTOCOL.md)). It needs only a network: the phone and the
  computer on the same LAN, Ethernet or Wi-Fi. It has been tested against an
  independent implementation in both directions.
* Discovery uses mDNS only, no Bluetooth. On a phone that also looks for
  receivers over Bluetooth this can matter; see
  [Troubleshooting](#troubleshooting).
* Android only. iPhones and iPads are not supported.

## Install

```bash
sudo pacman -S python-cryptography python-zeroconf   # required
sudo pacman -S libnotify wl-clipboard jq             # notifications, clipboard, menu

makepkg -si                                          # in this directory
systemctl --user enable --now archdropd
archdrop doctor                                      # tells you what is missing
```

Without installing, from this checkout: `bin/archdropd` and `bin/archdrop` run in
place. The service reads `WAYLAND_DISPLAY` from the session for `wl-copy`, which
Hyprland setups normally export to systemd with
`dbus-update-activation-environment --systemd WAYLAND_DISPLAY`.

## Use

```bash
archdrop on 10m            # become visible; "forever" or a bare number of minutes work too
archdrop off
archdrop peers             # devices that can receive from you right now
archdrop send --to Pixel photo.jpg notes.pdf
archdrop send --to Pixel --text "https://example.com"
archdrop send --pick --to Pixel   # choose the files in the desktop file chooser
archdrop send --clipboard  # files copied in a file manager, an image, or text
archdrop accept            # the pending offer; or `archdrop accept t3`
archdrop reject
archdrop status --json
```

Receiving: switch it on, open the share sheet on the phone, and pick the computer.
An offer waits for you, with the four-digit **code** that also shows on the phone.
Match them, then accept from the bar or with `archdrop accept`. Nothing is written
before you accept, files land in your Downloads folder (or `download_dir`) as
`name.part` and are renamed when complete, and a sender cannot choose a path: only
the file name is used.

Sending: on Android open Quick Share and choose **Everyone**, then
`archdrop send --to <name> ...`. A text or link arrives on the phone as a message.
Received text goes to the clipboard and is never opened for you.

## Waybar

Copy the module from `integrations/waybar/module.jsonc` and the rules from
`style.css` into your Waybar config (installed under `/usr/share/archdrop/waybar/`).
Left click switches receiving on and off, right click opens an action menu in
whichever of fuzzel, wofi, rofi, tofi, walker or bemenu you have, middle click
sends the clipboard to the only device in range. The icon shows the state
(off, visible, an offer waiting, a transfer with its percentage, or the service
being down) through CSS classes.

## impasto

```bash
archdrop-impasto            # or: integrations/impasto/install.py
```

This adds an **Archdrop** module to your impasto config and hooks the island:

* a service (`services/DropService.qml`) that follows the daemon's state stream;
* a module (`bar/modules/DropModule.qml`): the receive switch, a card to accept or
  decline an offer with its code, transfers in flight with a cancel button, and
  the nearby devices. Clicking a device opens the file chooser; the clipboard
  button on its row sends what you copied instead;
* in the resting island, an offer or a transfer appears beside the clock
  (`Accept?` or the percentage), and a new offer opens the detail once.

Then add Archdrop to your bar in **Settings → Bar**, or reload the shell. The five
impasto files it touches get small exact insertions; if impasto has changed so an
insertion no longer applies, nothing is modified. `--dry-run` shows the plan,
`--uninstall` removes exactly what was added, and originals are kept in
`~/.local/state/archdrop/impasto-backup/`. impasto's own updater treats those
files as edited by you and will leave its newer version beside them as `.new`;
run the installer again after an update.

## Configuration

`~/.config/archdrop/config.toml` (see `share/config.toml`): device name, download
folder, a fixed TCP `port` for firewalls, whether to be visible at login, how long
"on" lasts, `auto_accept` (off; it lets anyone who can see you write to your
downloads) and notifications. `archdrop set name "Study"`
changes the few settings the CLI may change.

Firewall: Quick Share listens on a TCP port and announces itself on mDNS
(UDP 5353). Behind ufw or nftables set `port = 47893` and allow that port and
5353/udp. `archdrop doctor` also reports missing tools.

## Troubleshooting

`archdrop doctor` checks the local setup. For the phone side:

* **The phone drops off Wi-Fi when the share sheet opens, and the computer never
  appears.** Recent Pixels and Galaxys do this while "Share with Apple devices" is
  on (Quick Share settings), and a phone that is off the network cannot see a
  receiver announced on it. Turn that option off. Without it, the phone can only
  find a receiver over Bluetooth, which needs a Bluetooth adapter on this machine
  and is not implemented.
* **The computer never appears in the phone's list.** Same network, no client
  isolation on the router, and the phone must be showing Quick Share's receive
  screen with visibility set to Everyone. Then run `archdrop trace` on the
  computer and open the share sheet: it prints whether the phone asks for
  receivers at all, and how often.
* **It appears greyed out, or disappears after a while.** The service announces
  itself every 10 seconds, because a phone that has not heard from a receiver
  within a couple of minutes forgets it, using packets identical to a receiver
  known to work (see `src/archdrop/mdns.py`). If it is still greyed out, the phone
  may be looking for receivers over Bluetooth, which this machine may not have;
  that is a known gap (below).
* **The phone never shows up in `archdrop peers`.** Sending to a phone needs the
  phone to advertise, and Android only advertises on the network after it hears a
  Bluetooth "someone wants to share" signal from the sender. A computer without
  Bluetooth cannot send that signal, so this direction (computer to phone) does
  not work there. Receiving from the phone does not need it. `archdrop trace` output, and the daemon's log
  (`journalctl --user -u archdropd`, which notes every connection attempt), say
  which case it is.

## Security model

* The receiver listens while the service runs, unless you switch it off
  (`archdrop off`, or `receive_on_start = false` in the config). `archdrop on 10m`
  limits a window to a time you choose.
* Every session is encrypted and authenticated (UKEY2, AES-256-CBC, HMAC-SHA256,
  replay-checked). Whoever can reach the port can start a session, so the consent
  prompt is the security boundary: nothing reaches disk, and no text is acted on,
  without your accept.
* Limits: 4 concurrent sessions, 20 s to finish the handshake, a 30 s wait for the
  offer, size and free-space checks (256 MB is always left free), bounded frame and
  payload sizes, at most 500 files per offer.
* The control socket is `$XDG_RUNTIME_DIR/archdrop.sock`, mode 0600.

## Development

```bash
python -m pytest tests          # 18 tests: codec, crypto, loopback, hostile input, daemon
bin/archdropd                   # run in the foreground; ARCHDROP_LOG=DEBUG for detail
```

Layout: `src/archdrop/` (`pb.py` protobuf codec, `messages.py` schemas,
`secure.py` UKEY2 and envelope, `channel.py` framing and handshake,
`quickshare.py` sessions and mDNS, `trace.py`, `daemon.py`, `cli.py`), `integrations/`, `systemd/`, `share/`, `docs/`.

## Not done

Bluetooth wake-up and Bluetooth/Wi-Fi Direct transports, contacts-only
visibility, sending folders.
