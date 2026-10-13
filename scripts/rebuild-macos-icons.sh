#!/bin/bash
# Re-encode Coinfly's existing icon artwork with native macOS tools.
set -euo pipefail

[[ "$(uname -s)" == Darwin ]] || {
    printf 'Run this script on macOS to use sips and iconutil.\n' >&2
    exit 1
}
coinfly_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
icon_directory="$coinfly_root/assets/icons"
master="$icon_directory/coinfly.png"
[[ -f "$master" ]] || { printf 'Missing icon master: %s\n' "$master" >&2; exit 1; }
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
iconset="$temporary/coinfly.iconset"
mkdir -p "$iconset"

for points in 16 32 128 256 512; do
    for scale in 1 2; do
        pixels=$((points * scale))
        suffix=""
        if [[ "$scale" == 2 ]]; then suffix="@2x"; fi
        name="icon_${points}x${points}${suffix}.png"
        /usr/bin/sips -z "$pixels" "$pixels" "$master" --out "$iconset/$name" >/dev/null
    done
done
/usr/bin/iconutil -c icns -o "$icon_directory/coinfly.icns" "$iconset"
printf 'Native macOS icon rebuilt: %s\n' "$icon_directory/coinfly.icns"
