#!/bin/sh
# Install the Ryujin II LCD dashboard service. Run from the repo folder: sudo sh install.sh
# Needs: python3, Pillow and pyusb (Fedora: python3-pillow python3-pyusb,
# Debian/Ubuntu: python3-pil python3-usb, Arch: python-pillow python-pyusb).
set -eu
cd "$(dirname "$0")"
python3 -c "import PIL, usb.core" 2>/dev/null || { echo "missing Pillow or pyusb - see README"; exit 1; }
getent group ryujin-lcd >/dev/null || groupadd -r ryujin-lcd
install -m 0755 ryujin-lcd-stream.py /usr/local/bin/ryujin-lcd-monitor
install -d -m 0755 /usr/local/share/ryujin-lcd
if [ -e /usr/local/share/ryujin-lcd/background.png ]; then
  echo "keeping your installed background (/usr/local/share/ryujin-lcd/background.png)"
else
  install -m 0644 assets/background.png /usr/local/share/ryujin-lcd/background.png
fi
if [ -e /etc/ryujin-lcd.conf ]; then
  echo "keeping your settings (/etc/ryujin-lcd.conf)"
else
  install -m 0644 ryujin-lcd.conf.example /etc/ryujin-lcd.conf
fi
install -m 0644 udev/60-ryujin-lcd.rules /etc/udev/rules.d/60-ryujin-lcd.rules
install -m 0644 systemd/ryujin-lcd-monitor.service /etc/systemd/system/ryujin-lcd-monitor.service
command -v restorecon >/dev/null && restorecon -R /usr/local/bin/ryujin-lcd-monitor /usr/local/share/ryujin-lcd \
  /etc/ryujin-lcd.conf /etc/udev/rules.d/60-ryujin-lcd.rules /etc/systemd/system/ryujin-lcd-monitor.service || true
udevadm control --reload
udevadm trigger --action=change --subsystem-match=hidraw
udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=0b05
sleep 1
ls -l /dev/hidraw* | grep -q ryujin-lcd || echo "WARNING: no hidraw node with group ryujin-lcd - is the cooler's USB cable connected?"
systemctl daemon-reload
systemctl enable ryujin-lcd-monitor.service
systemctl restart ryujin-lcd-monitor.service   # pick up a new script version
sleep 4
systemctl --no-pager status ryujin-lcd-monitor.service | head -12
journalctl -u ryujin-lcd-monitor --no-pager -n 5
