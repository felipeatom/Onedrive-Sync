#!/usr/bin/env bash
# Install script for Onedrive-Sync
set -e

echo "==> Installing Onedrive-Sync"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Create virtualenv and install
INSTALL_DIR="$HOME/.local/share/onedrive-sync"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip -q
"$INSTALL_DIR/venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt" -q
"$INSTALL_DIR/venv/bin/python" -m pip install "$SCRIPT_DIR" -q

# Wrapper script
cat > "$BIN_DIR/onedrive-sync" <<'EOF'
#!/usr/bin/env bash
exec "$HOME/.local/share/onedrive-sync/venv/bin/python" -m onedrive_atom.main "$@"
EOF
chmod +x "$BIN_DIR/onedrive-sync"

# Desktop entry
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cp "$SCRIPT_DIR/onedrive-sync.desktop" "$DESKTOP_DIR/"
chmod +x "$DESKTOP_DIR/onedrive-sync.desktop"
gio set "$DESKTOP_DIR/onedrive-sync.desktop" metadata::trusted true 2>/dev/null || true

# Install icon into hicolor theme
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/resources/icons/onedrive-sync.svg" "$ICON_DIR/onedrive-sync.svg"

# Install PNG fallbacks because some launchers do not render scalable SVG icons
# from user icon themes reliably until a full shell restart.
if command -v rsvg-convert >/dev/null 2>&1; then
  for size in 32 48 64 128 256 512; do
    PNG_DIR="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$PNG_DIR"
    rsvg-convert -w "$size" -h "$size" \
      "$SCRIPT_DIR/resources/icons/onedrive-sync.svg" \
      -o "$PNG_DIR/onedrive-sync.png"
  done
elif [ -d "$SCRIPT_DIR/resources/icons/png" ]; then
  for png in "$SCRIPT_DIR"/resources/icons/png/onedrive-sync-*.png; do
    [ -e "$png" ] || continue
    size="$(basename "$png" | sed -E 's/.*-([0-9]+)\.png/\1/')"
    PNG_DIR="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$PNG_DIR"
    cp "$png" "$PNG_DIR/onedrive-sync.png"
  done
fi

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
xdg-desktop-menu forceupdate 2>/dev/null || true

# Systemd user service
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"
cp "$SCRIPT_DIR/systemd/onedrive-sync.service" "$SERVICE_DIR/"
systemctl --user daemon-reload

echo ""
echo "==> Installation complete!"
echo ""
echo "To start Onedrive-Sync:"
echo "  onedrive-sync"
echo ""
echo "To enable the systemd service (headless mode):"
echo "  systemctl --user enable --now onedrive-sync.service"
echo ""
echo "Make sure $BIN_DIR is in your PATH."
