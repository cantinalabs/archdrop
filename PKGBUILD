# Maintainer: Dallin <dallinbuckles@protonmail.com>
pkgname=archdrop
pkgver=0.1.0
pkgrel=1
pkgdesc='Android Quick Share as a user service, with Waybar and impasto integrations'
arch=(any)
license=(custom)
depends=(python python-cryptography python-zeroconf)
optdepends=(
  'libnotify: desktop notifications'
  'wl-clipboard: send the clipboard, copy received text'
  'jq: the Waybar action menu'
  'fuzzel: launcher for the Waybar action menu (or wofi, rofi, tofi, walker, bemenu)'
  'waybar: bar module'
)
# Built from the working tree this file sits in.
_src=$startdir

check() {
  cd "$_src"
  python -m pytest -q tests
}

package() {
  cd "$_src"
  local site
  site=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')

  install -d "$pkgdir$site"
  cp -r src/archdrop "$pkgdir$site/"
  find "$pkgdir$site/archdrop" -name __pycache__ -prune -exec rm -rf {} +

  for tool in archdrop archdropd; do
    printf '#!/usr/bin/python\nfrom archdrop.%s import main\nmain()\n' \
      "$([ $tool = archdrop ] && echo cli || echo daemon)" > "$tool"
    install -Dm755 "$tool" "$pkgdir/usr/bin/$tool"
    rm "$tool"
  done
  install -Dm755 integrations/waybar/archdrop-menu "$pkgdir/usr/bin/archdrop-menu"
  printf '#!/bin/sh\nexec python3 /usr/share/archdrop/impasto/install.py "$@"\n' > archdrop-impasto
  install -Dm755 archdrop-impasto "$pkgdir/usr/bin/archdrop-impasto"
  rm archdrop-impasto

  install -Dm644 systemd/archdropd.service "$pkgdir/usr/lib/systemd/user/archdropd.service"
  install -Dm644 src/archdrop/icons/archdrop.svg "$pkgdir/usr/share/pixmaps/archdrop.svg"
  install -Dm644 share/archdrop-send.desktop "$pkgdir/usr/share/applications/archdrop-send.desktop"
  install -Dm644 share/config.toml "$pkgdir/usr/share/doc/archdrop/config.toml.example"
  install -Dm644 README.md "$pkgdir/usr/share/doc/archdrop/README.md"
  install -Dm644 docs/PROTOCOL.md "$pkgdir/usr/share/doc/archdrop/PROTOCOL.md"

  install -Dm644 integrations/waybar/module.jsonc "$pkgdir/usr/share/archdrop/waybar/module.jsonc"
  install -Dm644 integrations/waybar/style.css "$pkgdir/usr/share/archdrop/waybar/style.css"
  install -Dm755 integrations/impasto/install.py "$pkgdir/usr/share/archdrop/impasto/install.py"
  cp -r integrations/impasto/files "$pkgdir/usr/share/archdrop/impasto/files"
}
