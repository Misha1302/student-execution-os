#!/usr/bin/env bash
# Regenerates launcher icons and splash screens from resources/*.svg.
# Needs rsvg-convert and ImageMagick (magick). Output is committed, so this only
# has to run when the artwork changes.
set -euo pipefail
cd "$(dirname "$0")/.."
res=android/app/src/main/res
declare -A launcher=([mdpi]=48 [hdpi]=72 [xhdpi]=96 [xxhdpi]=144 [xxxhdpi]=192)
declare -A fg=([mdpi]=108 [hdpi]=162 [xhdpi]=216 [xxhdpi]=324 [xxxhdpi]=432)
for d in "${!launcher[@]}"; do
  s=${launcher[$d]}
  rsvg-convert -w "$s" -h "$s" resources/icon.svg -o "$res/mipmap-$d/ic_launcher.png"
  magick -size "${s}x${s}" xc:none -fill white -draw "circle $((s/2)),$((s/2)) $((s/2)),0" \
    \( resources/icon.svg -background none -resize "${s}x${s}" \) -compose SrcIn -composite \
    "$res/mipmap-$d/ic_launcher_round.png" 2>/dev/null || \
    rsvg-convert -w "$s" -h "$s" resources/icon.svg -o "$res/mipmap-$d/ic_launcher_round.png"
  f=${fg[$d]}
  rsvg-convert -w "$f" -h "$f" resources/icon-foreground.svg -o "$res/mipmap-$d/ic_launcher_foreground.png"
done
logo=$(mktemp --suffix=.png)
rsvg-convert -w 288 -h 288 resources/icon.svg -o "$logo"
for f in $(find "$res" -name splash.png); do
  size=$(magick identify -format '%wx%h' "$f")
  magick -size "$size" xc:'#f6f7f9' "$logo" -gravity center -composite "$f"
done
rm -f "$logo"
echo "icons and splash regenerated"
