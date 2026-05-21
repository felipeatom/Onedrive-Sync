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
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# Install icon into hicolor theme
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/resources/icons/onedrive-sync.svg" "$ICON_DIR/onedrive-sync.svg"
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

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
