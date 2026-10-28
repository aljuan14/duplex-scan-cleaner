#!/usr/bin/env bash
# Pasang perintah `bersihkan-scan` dan ikon "Pembersih Scan" di menu aplikasi.
set -e
APP="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

missing=()
for cmd in python3 curl pdftoppm pdfseparate pdfunite pdfinfo; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
python3 -c "import PIL" 2>/dev/null || missing+=("python3-pillow")
if [ ${#missing[@]} -gt 0 ]; then
  echo "Program berikut belum terpasang: ${missing[*]}"
  echo "  Arch/Manjaro : sudo pacman -S python python-pillow poppler curl"
  echo "  Debian/Ubuntu: sudo apt install python3 python3-pil poppler-utils curl"
  echo "  Fedora       : sudo dnf install python3 python3-pillow poppler-utils curl"
  exit 1
fi

chmod +x "$APP/bersihkan-scan"
mkdir -p "$BIN" "$DESKTOP"
ln -sf "$APP/bersihkan-scan" "$BIN/bersihkan-scan"

cat > "$DESKTOP/pembersih-scan.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pembersih Scan
Comment=Buang halaman kosong dari PDF hasil scan bolak-balik
Exec=$BIN/bersihkan-scan
Icon=edit-clear
Terminal=false
Categories=Office;Utility;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP" 2>/dev/null || true

echo "Terpasang."
echo "  Jalankan  : bersihkan-scan   (atau buka 'Pembersih Scan' dari menu aplikasi)"
echo "  Matikan   : bersihkan-scan stop"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Catatan: tambahkan $BIN ke PATH agar perintah bersihkan-scan dikenali.";; esac
