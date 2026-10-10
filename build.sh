#!/usr/bin/env bash
# =====================================================================
#  build.sh — сборка VoiceChanger в единый портативный AppImage.
#  Использование:  ./build.sh
#  Результат:      VoiceChanger-x86_64.AppImage в корне проекта.
# =====================================================================
set -euo pipefail

APP_NAME="VoiceChanger"
APP_ID="voicechanger"
ARCH="$(uname -m)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$ROOT/AppDir"
TOOL="$ROOT/appimagetool-$ARCH.AppImage"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

PY=python3

# ---------- 1. Python-зависимости (PyQt6, numpy, sounddevice, pyinstaller) --
if ! "$PY" -c "import PyQt6, numpy, sounddevice, PyInstaller" 2>/dev/null; then
    warn "Зависимостей не хватает — создаю/использую .venv…"
    [ -x "$ROOT/.venv/bin/python" ] || "$PY" -m venv --system-site-packages "$ROOT/.venv" \
        || die "Нужен пакет python3-venv: sudo apt install python3-venv"
    "$ROOT/.venv/bin/pip" install -q PyQt6 numpy sounddevice pyinstaller
    PY="$ROOT/.venv/bin/python"
fi
"$PY" -c "import PyQt6, numpy, sounddevice, PyInstaller" \
    || die "Не удалось подготовить зависимости"
log "Python: $PY (PyQt6, numpy, sounddevice, PyInstaller OK)"

# ---------- 2. appimagetool (лежит в корне проекта, как на образце) ---------
if [ ! -x "$TOOL" ]; then
    warn "appimagetool не найден — скачиваю в корень проекта…"
    URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$TOOL" "$URL"
    elif command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$TOOL" "$URL"
    else
        die "Нужен wget или curl"
    fi
    chmod +x "$TOOL"
fi
log "appimagetool: $TOOL"

# ---------- 3. Иконка (icon.png в корне; если нет — генерируем) -------------
if [ ! -f "$ROOT/icon.png" ]; then
    log "icon.png не найден — генерирую (256×256, микрофон)…"
    "$PY" - "$ROOT/icon.png" <<'EOF'
import struct, sys, zlib
SIZE = 256
def row(y):
    r_ = bytearray(SIZE * 4)
    for x in range(SIZE):
        t = y / (SIZE - 1)
        r, g, b = (int(0x6C + (0x48 - 0x6C) * t),
                   int(0x5C + (0x34 - 0x5C) * t),
                   int(0xE7 + (0xD4 - 0xE7) * t))
        rad = 48
        dx = max(rad - x, x - (SIZE - 1 - rad), 0)
        dy = max(rad - y, y - (SIZE - 1 - rad), 0)
        if dx*dx + dy*dy > rad*rad:
            r_[x*4:x*4+4] = bytes((0, 0, 0, 0)); continue
        white = (128-27 <= x <= 128+27 and 79 <= y <= 115) or \
                ((x-128)**2 + (y-79)**2 <= 27**2) or \
                ((x-128)**2 + (y-115)**2 <= 27**2)
        d2 = (x-128)**2 + (y-106)**2
        white = white or (not white and 46**2 <= d2 <= 56**2 and y >= 104) or \
                (not white and 122 <= x <= 134 and 150 <= y <= 184) or \
                (not white and 96 <= x <= 160 and 184 <= y <= 194)
        if white: r = g = b = 255
        r_[x*4:x*4+4] = bytes((r, g, b, 255))
    return bytes(r_)
def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + \
           struct.pack(">I", zlib.crc32(tag + data))
raw = b"".join(b"\x00" + row(y) for y in range(SIZE))
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
open(sys.argv[1], "wb").write(png)
EOF
fi
[ -f "$ROOT/icon.png" ] || die "Нет icon.png"
log "Иконка: $ROOT/icon.png"

# ---------- 4. PyInstaller: app.py → один каталог ---------------------------
log "Очистка предыдущих сборок…"
rm -rf "$ROOT/build" "$ROOT/dist" "$APPDIR"
mkdir -p "$APPDIR/usr"

log "PyInstaller: собираю app.py в каталог…"
(cd "$ROOT" && "$PY" -m PyInstaller \
    --noconfirm --clean --onedir --windowed \
    --workpath "$ROOT/build" --distpath "$ROOT/dist" --specpath "$ROOT" \
    --name "$APP_NAME" app.py)

[ -d "$ROOT/dist/$APP_NAME" ] || die "PyInstaller не создал dist/$APP_NAME"

log "Копирую сборку в AppDir/usr/$APP_NAME…"
cp -a "$ROOT/dist/$APP_NAME" "$APPDIR/usr/$APP_NAME"
find "$APPDIR/usr/$APP_NAME" -type f \( -name '*.so*' -o -name "$APP_NAME" \) \
     -exec strip --strip-unneeded {} \; 2>/dev/null || true

# ---------- 5. Сборка AppDir (usr, AppRun, desktop, icon) -------------------
cp "$ROOT/AppRun" "$APPDIR/AppRun"
cp "$ROOT/voicechanger.desktop" "$APPDIR/"
cp "$ROOT/icon.png" "$APPDIR/$APP_ID.png"
ln -sf "$APP_ID.png" "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "$ROOT/icon.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
chmod +x "$APPDIR/AppRun"

# ---------- 6. Упаковка ------------------------------------------------------
OUT="$ROOT/${APP_NAME}-${ARCH}.AppImage"
rm -f "$OUT"
log "appimagetool: упаковываю AppDir → $OUT"
if [[ "$TOOL" == *.AppImage ]]; then
    "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
else
    "$TOOL" "$APPDIR" "$OUT"
fi
chmod +x "$OUT"

log "Готово: $OUT"
echo  "    Запуск:      \"$OUT\""
echo  "    Без FUSE:    \"$OUT\" --appimage-extract-and-run"
