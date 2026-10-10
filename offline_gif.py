#!/usr/bin/env python3
"""Show your own picture on the Ryujin II LCD while the PC is OFF (instead of the ROG animation).

The cooler keeps its screen powered while the PC is off (as long as the PSU has power) and
keeps showing the last mode it was told to play. Live frames need the PC, so the picture
has to be STORED on the cooler: upload it once as a GIF, then ryujin-lcd-stream.py plays it
on stop (OFFLINE_GIF_SLOT in /etc/ryujin-lcd.conf). Upload sequence from
github.com/zaclanzon/ryujin-iii-lcd-linux ryujin_lcd/device.py (same S750 controller):

  EC 71 01 01 (+event EE 12), EC F1      refresh + read storage table
  EC 72 01 02 <slot>                     select GIF slot (file type 02 = GIF)
  EC 73 01 (+event EE 13 00 01)          begin write
  EC 7F 02 <size u32 LE>                 announce size, reply carries chunk size
  bulk OUT 0x01, chunks                  file data, zero-padded, event EE 14 00 00 after each
  EC 73 FF (+event EE 13 00 FF)          end write
  EC 5D 00 01 10 01 <slot> 05            one-entry play list: the GIF
  EC 51 10 01 <slot>                     play the stored GIF

Ryujin II rules found by testing: the GIF must be 320x240 (a 640x480 one is not shown) and
NOT interlaced (Pillow interlaces by default; the firmware then shows 4 squashed copies).
Storage table byte 17 = GIF slot map (bit n = slot n in use), byte 15 = JPG slots.

Hard limits (asserted on every packet): only the commands above plus 51 14 00 00 (ROG
animation), 72 only for GIF slots 0-9, 73 only 01 / FF (73 03 = delete is impossible),
refuses a GIF slot the table says is in use. Never pump/fan, never format.

  python3 offline_gif.py make [picture]          # offline.gif + preview-offline.png (no device)
                                                 # default picture: the installed dashboard background
  sudo systemctl disable --now ryujin-lcd-monitor   # after live streaming the cooler ignores
  sudo poweroff                                     # all commands until it loses power: full power-off
  sudo python3 offline_gif.py slots              # which GIF slots are free
  sudo python3 offline_gif.py upload 0           # store offline.gif in a FREE GIF slot
  sudo python3 offline_gif.py play 0             # check it on the screen
  sudo python3 offline_gif.py rog                # back to the ROG animation
  # set OFFLINE_GIF_SLOT=0 in /etc/ryujin-lcd.conf, then:
  sudo systemctl enable --now ryujin-lcd-monitor
"""
import glob
import io
import os
import select
import struct
import sys
import time

VID, PID = 0x0B05, 0x1988
CMD, EVENT = 0xEC, 0xEE
CHUNK = 4096
HERE = os.path.dirname(os.path.abspath(__file__))
GIF = os.path.join(HERE, "offline.gif")
DEFAULT_PICTURE = "/usr/local/share/ryujin-lcd/background.png"
REPLY_ID = {0x82: 0x02, 0xF1: 0x71}


def check(payload):
    """Every command goes through here. Anything outside the GIF sequence is refused."""
    p = bytes(payload)
    ok = (p[:1] == b"\x82" or p[:1] == b"\xF1" or p[:3] == b"\x71\x01\x01"
          or (p[:3] == b"\x72\x01\x02" and len(p) == 4 and p[3] <= 9)
          or p in (b"\x73\x01", b"\x73\xFF", b"\x51\x14\x00\x00")
          or (p[:2] == b"\x7F\x02" and len(p) == 6)
          or (p[:5] == b"\x5D\x00\x01\x10\x01" and len(p) == 7 and p[5] <= 9 and p[6] == 5)
          or (p[:3] == b"\x51\x10\x01" and len(p) == 4 and p[3] <= 9))
    assert ok, f"refused command {p.hex(' ')}"
    return p


class Dev:
    def __init__(self):
        path = None
        for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            if "0B05:1988" in os.path.realpath(os.path.join(node, "device")).upper():
                path = "/dev/" + os.path.basename(node)
        if not path:
            sys.exit("Ryujin II (0b05:1988) not found")
        self.fd = os.open(path, os.O_RDWR)
        self.events = []
        self.usb = None

    def _read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        rep = os.read(self.fd, 65)
        if rep[:1] == bytes([EVENT]):
            print(f"    event {rep[:6].hex(' ')}")
            self.events.append(rep)
        return rep

    def cmd(self, payload, timeout=3.0):
        p = check(payload)
        print(f"  -> EC {p.hex(' ')}")
        os.write(self.fd, bytes([CMD]) + p.ljust(64, b"\0"))
        want = REPLY_ID.get(p[0], p[0])
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            rep = self._read(left)
            if rep and rep[0] == CMD and rep[1] == want:
                print(f"  <- {rep[:12].hex(' ')}")
                return rep
        raise OSError(f"no reply to EC {p[0]:02X}")

    def wait_event(self, prefix, timeout=5.0):
        end = time.monotonic() + timeout
        while True:
            for i, ev in enumerate(self.events):
                if all(b is None or ev[1 + j] == b for j, b in enumerate(prefix)):
                    return self.events.pop(i)
            left = end - time.monotonic()
            if left <= 0:
                return None
            self._read(left)

    def bulk(self, data):
        import usb.core
        if self.usb is None:
            self.usb = usb.core.find(idVendor=VID, idProduct=PID)
            if self.usb is None:
                raise OSError("USB device not found for bulk write")
        n = self.usb.write(0x01, data, timeout=5000)   # interface 0, no kernel driver on it
        if n != len(data):
            raise OSError(f"short bulk write {n}/{len(data)}")
        print(f"  => bulk {n} bytes")

    def _refresh(self):
        try:
            self.cmd(b"\x71\x01\x01", timeout=1.0)
        except OSError:
            print("  (no EC 71 reply - waiting for the EE 12 event instead)")
        return self.wait_event((0x12,), timeout=2.0)

    def table(self):
        """Storage table. While a GIF plays, EC 71 may get only the EE 12 event, no reply.
        After live streaming the cooler ignores every command until it loses power."""
        if not self._refresh():
            raise OSError("no answer from the cooler - if the dashboard streamed since the last power-off: "
                          "systemctl disable --now ryujin-lcd-monitor, power off, switch on, try again")
        self.events.clear()                      # drop duplicate EE 12 events
        d = self.cmd(b"\xF1")
        u32 = lambda i: int.from_bytes(d[i:i + 4], "little")
        return u32(4), u32(8), d

def rotation():
    """ROTATE from the dashboard's config, so the stored picture is turned like the live screen."""
    try:
        for line in open(os.environ.get("RYUJIN_LCD_CONF", "/etc/ryujin-lcd.conf")):
            k, _, v = line.split("#", 1)[0].partition("=")
            if k.strip().upper() == "ROTATE" and v.strip():
                return int(v.strip().strip("\"'"))
    except OSError:
        pass
    return 270


def make(picture=DEFAULT_PICTURE):
    """320x240 GIF of the picture as it looks on the dashboard (cropped to fit, turned by ROTATE)."""
    from PIL import Image, ImageOps
    rot = rotation()
    size = (240, 320) if rot % 180 else (320, 240)             # what you see on the screen
    view = ImageOps.fit(Image.open(picture).convert("RGB"), size, Image.LANCZOS)
    im = view.rotate(rot, expand=True) if rot else view       # what the panel gets
    out = io.BytesIO()
    im.quantize(256, dither=Image.Dither.NONE).save(out, "GIF", interlace=False)   # firmware ignores interlacing
    open(GIF, "wb").write(out.getvalue())
    view.save(os.path.join(HERE, "preview-offline.png"))
    print(f"wrote {GIF} ({len(out.getvalue())} bytes, {im.size[0]}x{im.size[1]}) + preview-offline.png")


def slots():
    total, free, d = Dev().table()
    print(f"storage: total {total} KB, free {free} KB")
    print("GIF slots in use:", [n for n in range(8) if d[17] >> n & 1] or "none")
    print("JPG slots in use:", [n for n in range(8) if d[15] >> n & 1] or "none")


def upload(slot, path=GIF):
    data = open(path, "rb").read()
    assert data[:6] in (b"GIF87a", b"GIF89a") and len(data) < 1_000_000, "not a GIF or too big"
    dev = Dev()
    total, free, before = dev.table()
    print(f"storage: total {total} KB, free {free} KB")
    if before[17] >> slot & 1:                 # Ryujin II: byte 17 = GIF slot map
        sys.exit(f"GIF slot {slot} is in use (byte 17 = {before[17]:02x}) - pick a free one")
    begun = False
    try:
        dev.cmd(bytes([0x72, 0x01, 0x02, slot]))                # select GIF slot
        begun = True                                            # close it even if the reply is lost
        dev.cmd(b"\x73\x01")
        if not dev.wait_event((0x13, 0x00, 0x01)):
            raise OSError("no begin-write acknowledgement (EE 13 00 01)")
        r = dev.cmd(b"\x7F\x02" + struct.pack("<I", len(data)))
        chunk = struct.unpack("<H", r[3:5])[0] or CHUNK
        if not 1 <= chunk <= CHUNK:
            raise OSError(f"unexpected chunk size {chunk}")
        for off in range(0, len(data), chunk):
            dev.bulk(data[off:off + chunk].ljust(chunk, b"\0"))
            if not dev.wait_event((0x14, 0x00, 0x00)):
                raise OSError("no chunk acknowledgement (EE 14 00 00)")
        dev.cmd(b"\x73\xFF")
        begun = False
        if not dev.wait_event((0x13, 0x00, 0xFF)):
            print("  (no end-write event, checking table anyway)")
    finally:
        if begun:
            print("  closing the unfinished write (73 FF)")
            dev.cmd(b"\x73\xFF")
            dev.wait_event((0x13, 0x00, 0xFF))
    total, free, after = dev.table()
    print(f"storage after: total {total} KB, free {free} KB, GIF slot map {before[17]:02x} -> {after[17]:02x}")


def send(dev, payload):
    """Display commands may not be answered - a missing reply is not an error here."""
    try:
        dev.cmd(payload, timeout=1.0)
    except OSError as e:
        print(f"  ({e} - fine for display commands)")


def play(slot):
    dev = Dev()
    send(dev, bytes([0x5D, 0x00, 0x01, 0x10, 0x01, slot, 0x05]))
    send(dev, bytes([0x51, 0x10, 0x01, slot]))
    print("sent - is your picture on the screen, upright?")


def rog():
    send(Dev(), b"\x51\x14\x00\x00")


def main():
    a = sys.argv[1:]
    if a[:1] == ["make"] and len(a) <= 2:
        return make(*a[1:])
    if a == ["rog"]:
        return rog()
    if a == ["slots"]:
        return slots()
    if len(a) == 2 and a[0] in ("upload", "play") and a[1].isdigit() and int(a[1]) <= 7:
        return (upload if a[0] == "upload" else play)(int(a[1]))
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
