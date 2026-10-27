#!/bin/bash
# RAID Forge installer for Raspberry Pi OS (Bookworm / Trixie) on Pi 5 or Pi 500
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/raidforge

echo "== Installing RAID Forge =="
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-tk mdadm smartmontools xfsprogs pciutils

sudo mkdir -p "$DEST"
sudo cp "$DIR/raidforge.py" "$DEST/raidforge.py"
sudo cp "$DIR/raidforge.svg" "$DEST/raidforge.svg"
sudo chmod 755 "$DEST/raidforge.py"

sudo tee /usr/share/applications/raidforge.desktop >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=RAID Forge
Comment=Build and manage RAID 0/1/5/6/10 arrays on a drive HAT
Exec=python3 $DEST/raidforge.py
Icon=$DEST/raidforge.svg
Terminal=false
Categories=System;Utility;
EOF

sudo tee /usr/share/applications/raidforge-demo.desktop >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=RAID Forge (Demo)
Comment=Try RAID Forge with fake drives — changes nothing
Exec=python3 $DEST/raidforge.py --demo
Icon=$DEST/raidforge.svg
Terminal=false
Categories=System;Utility;
EOF

if [ -d "$HOME/Desktop" ]; then
    cp /usr/share/applications/raidforge.desktop "$HOME/Desktop/"
    chmod +x "$HOME/Desktop/raidforge.desktop"
    gio set "$HOME/Desktop/raidforge.desktop" metadata::trusted true 2>/dev/null || true
fi

echo
echo "Done. Open it from Menu > System Tools > RAID Forge (or the Desktop icon)."
echo "Try it safely first:  python3 $DEST/raidforge.py --demo"
