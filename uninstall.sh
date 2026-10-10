#!/bin/sh
# Remove the service and put the ROG animation back. Run from the repo folder: sudo sh uninstall.sh
# Keeps /etc/ryujin-lcd.conf and /usr/local/share/ryujin-lcd/ (delete them by hand if you want).
set -u
cd "$(dirname "$0")"
systemctl disable --now ryujin-lcd-monitor.service
python3 offline_gif.py rog || echo "could not switch to the ROG animation (cooler not found?)"
rm -f /etc/systemd/system/ryujin-lcd-monitor.service /usr/local/bin/ryujin-lcd-monitor /etc/udev/rules.d/60-ryujin-lcd.rules
systemctl daemon-reload
udevadm control --reload
echo "removed. Settings and background kept in /etc/ryujin-lcd.conf and /usr/local/share/ryujin-lcd/"
