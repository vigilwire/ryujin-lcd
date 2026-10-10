#!/usr/bin/env python3
"""Hacker-style live dashboard (CPU / CPU temp / GPU / VRAM / RAM) streamed to the ASUS ROG Ryujin II LCD.

The PC draws the whole screen with Pillow and streams it as raw frames: the cooler's own
hardware-monitor page can only draw one big line. Frames go straight to the panel, so
nothing is written to the cooler's storage. Look: green terminal style over a background
picture (make_background.py), falling hex "Matrix" code that moves every frame, mono stats
with glow and [###...] bars.

Protocol (Ryujin II, USB 0b05:1988, firmware AURJ1-S750-0104), from
github.com/zenstrukt/signalrgb-device-plugins:

  EC 51 20 00 00                  enter live mode
  EC 7F 03 00 84 03 00            announce a 230400-byte frame (320*240*3)
  bulk OUT 0x01                   frame as BGR888, 4096-byte writes, last one EXACT length
  EC 7F 03 00 84 03 00            commit this frame + announce the next

If frames stop, the cooler falls back to its stored animation, so a frame is sent FPS times
a second (numbers are re-read once a second). The cooler does NOT answer any HID request
while streaming, so nothing waits for a reply. Survives the device disappearing (suspend,
replug). On stop (shutdown, reboot, systemctl stop) it plays the GIF stored in
OFFLINE_GIF_SLOT (see offline_gif.py) - the cooler keeps its screen powered while the PC is
off - or, by default, the ROG animation.

Only EC 51 20 00 00, 51 14 00 00, 7F 03 00 84 03 00 and the two GIF play commands
(5D play list, 51 10 01 <slot>) are ever sent: never pump/fan duty, never file operations.

Settings: /etc/ryujin-lcd.conf (KEY=value, see ryujin-lcd.conf.example), or the file
named in $RYUJIN_LCD_CONF.
"""
import glob
import os
import random
import select
import signal
import sys
import time

import usb.core
import usb.util
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


# --- settings ---------------------------------------------------------------------
def read_conf(path):
    conf = {}
    try:
        for line in open(path):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip().upper()] = v.strip().strip('"\'')
    except OSError:
        pass
    return conf


CONF = read_conf(os.environ.get("RYUJIN_LCD_CONF", "/etc/ryujin-lcd.conf"))


def setting(key, default, kind=str):
    v = CONF.get(key, "")
    if v == "":
        return default
    if kind is int and v.lower() == "none":
        return None
    return kind(v)


GPU_PCI = setting("GPU_PCI", "auto")      # e.g. 0000:03:00.0; auto = AMD GPU with the most VRAM
FPS = setting("FPS", 10, int)
ROTATE = setting("ROTATE", 270, int)      # degrees counter-clockwise: panel is mounted sideways (portrait)
BACKGROUND = setting("BACKGROUND", "/usr/local/share/ryujin-lcd/background.png")
FONT = setting("FONT", "auto")
RAIN = setting("RAIN", 0.6, float)        # falling-code brightness (0 = off)
EDGE = setting("EDGE", 14, int)           # px kept free along the screen frame
OFFLINE_GIF_SLOT = setting("OFFLINE_GIF_SLOT", None, int)   # GIF shown while the PC is off; None = ROG animation

VID, PID = 0x0B05, 0x1988
W, H = 320, 240            # panel frame; we draw on a portrait H x W canvas and rotate
CW, CH = (H, W) if ROTATE % 180 else (W, H)
FRAME = W * H * 3          # 230400
CHUNK = 4096
LIVE = b"\x51\x20\x00\x00"
ROG_ANIMATION = b"\x51\x14\x00\x00"
ANNOUNCE = bytes([0x7F, 0x03]) + FRAME.to_bytes(4, "little")   # 7F 03 00 84 03 00
if OFFLINE_GIF_SLOT is not None and not 0 <= OFFLINE_GIF_SLOT <= 9:
    sys.exit(f"OFFLINE_GIF_SLOT must be 0..9 or None, not {OFFLINE_GIF_SLOT}")
PLAY_LIST = bytes([0x5D, 0x00, 0x01, 0x10, 0x01, OFFLINE_GIF_SLOT or 0, 0x05])   # one entry: the GIF
PLAY_GIF = bytes([0x51, 0x10, 0x01, OFFLINE_GIF_SLOT or 0])
ALLOWED = {LIVE, ROG_ANIMATION, ANNOUNCE, PLAY_LIST, PLAY_GIF}
# Colour-blind friendly: labels/units/bars (amber) are told apart from the numbers by size
# and BRIGHTNESS, not hue, and temperature warnings carry a text tag (WARM / HOT!).
NEON = (90, 255, 140)                       # numbers
SMALL = (255, 190, 40)                      # amber: labels, units, bars
LABEL, UNIT, BARC = SMALL, SMALL, SMALL
WARM, HOT = (255, 230, 0), (255, 255, 255)  # yellow, then white + "HOT!" tag


def log(*a):
    print(*a, flush=True)


# --- sensors ----------------------------------------------------------------------
def cpu_mhz():
    """Fastest core right now (what the CPU boosts to); idle cores sit much lower."""
    khz = []
    for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            khz.append(int(open(f).read()))
        except (OSError, ValueError):
            pass
    return max(khz) // 1000 if khz else None


def _find_cpu_temp():
    """AMD: k10temp temp1 (Tctl). Intel: coretemp "Package id 0"."""
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(f"{h}/name").read().strip()
            if name == "k10temp":
                return f"{h}/temp1_input"
            if name == "coretemp":
                for lf in glob.glob(f"{h}/temp*_label"):
                    if open(lf).read().strip().startswith("Package"):
                        return lf.replace("_label", "_input")
        except OSError:
            pass
    return None


_cpu_temp_path = None


def cpu_temp():
    global _cpu_temp_path
    if not (_cpu_temp_path and os.path.exists(_cpu_temp_path)):
        _cpu_temp_path = _find_cpu_temp()
    try:
        return round(int(open(_cpu_temp_path).read()) / 1000) if _cpu_temp_path else None
    except (OSError, ValueError):
        return None


def _find_gpu():
    """The amdgpu device with the most VRAM, i.e. the graphics card, not the iGPU."""
    if GPU_PCI != "auto":
        return f"/sys/bus/pci/devices/{GPU_PCI}"
    best, size = None, 0
    for f in glob.glob("/sys/bus/pci/devices/*/mem_info_vram_total"):
        try:
            n = int(open(f).read())
        except (OSError, ValueError):
            continue
        if n > size:
            best, size = os.path.dirname(f), n
    return best


GPU = _find_gpu()
_gpu_temp_path = None


def gpu_temp():
    global _gpu_temp_path
    if not (_gpu_temp_path and os.path.exists(_gpu_temp_path)):
        _gpu_temp_path = None
        for h in glob.glob(f"{GPU}/hwmon/hwmon*") if GPU else []:
            for lf in glob.glob(f"{h}/temp*_label"):
                if open(lf).read().strip() == "edge":
                    _gpu_temp_path = lf.replace("_label", "_input")
    try:
        return round(int(open(_gpu_temp_path).read()) / 1000) if _gpu_temp_path else None
    except (OSError, ValueError):
        return None


def gpu_vram():
    try:
        used = int(open(f"{GPU}/mem_info_vram_used").read()) / 2**30
        total = int(open(f"{GPU}/mem_info_vram_total").read()) / 2**30
        return used, total
    except (OSError, ValueError, TypeError):
        return None, None


def _hwmon_in(name_prefix, label=None, index=0, device=None):
    """Millivolts from an hwmon voltage input, or None."""
    pattern = f"{device}/hwmon/hwmon*" if device else "/sys/class/hwmon/hwmon*"
    for h in glob.glob(pattern):
        try:
            if not open(f"{h}/name").read().strip().startswith(name_prefix):
                continue
            if label:
                for lf in glob.glob(f"{h}/in*_label"):
                    if open(lf).read().strip() == label:
                        return int(open(lf.replace("_label", "_input")).read())
            elif os.path.exists(f"{h}/in{index}_input"):
                return int(open(f"{h}/in{index}_input").read())
        except (OSError, ValueError):
            pass
    return None


def cpu_volts():
    """Vcore = in0 of a Nuvoton Super I/O (driver nct6775, used on most ASUS boards).
    Shows -- until that driver is loaded."""
    mv = _hwmon_in("nct67", index=0)
    return mv / 1000 if mv else None


def gpu_volts():
    if not GPU:
        return None
    mv = _hwmon_in("amdgpu", label="vddgfx", device=GPU)
    return mv / 1000 if mv else None


_ram_mts = None


def ram_speed():
    """Configured memory speed (MT/s) from the DMI data udev keeps (readable without root)."""
    global _ram_mts
    if _ram_mts is None:
        try:
            for line in open("/run/udev/data/+dmi:id"):
                if "_CONFIGURED_SPEED_MTS=" in line:
                    _ram_mts = int(line.split("=", 1)[1])
                    break
        except (OSError, ValueError):
            pass
    return _ram_mts


def ram_percent():
    """Used RAM like `free` shows it: total minus available (cache counts as free)."""
    try:
        m = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
        return 100 * (m["MemTotal"] - m["MemAvailable"]) / m["MemTotal"]
    except (OSError, ValueError, KeyError):
        return None


def temp_state(c, warm=60, hot=80):
    """(number color, tag) - the tag is what makes the warning readable without colour."""
    if c is None or c < warm:
        return NEON, ""
    return (WARM, " WARM") if c < hot else (HOT, " HOT!")


def stats():
    """[(label, extra beside the label, number, unit, color, bar 0..1)]"""
    mhz, ct, t, ram = cpu_mhz(), cpu_temp(), gpu_temp(), ram_percent()
    used, total = gpu_vram()
    c_color, c_tag = temp_state(ct, 75, 90)    # CPUs run warmer than GPUs
    t_color, t_tag = temp_state(t)
    volts = lambda v: f"{v:.3f} V" if v else "-- V"
    mts = ram_speed()
    return [
        ("cpu", volts(cpu_volts()), str(mhz) if mhz else "--", "MHz", NEON,
         min(mhz / 6000, 1) if mhz else 0),
        ("cpu temp" + c_tag, "", str(ct) if ct is not None else "--", "°C", c_color,
         min(ct / 100, 1) if ct else 0),
        ("gpu" + t_tag, volts(gpu_volts()), str(t) if t is not None else "--", "°C", t_color,
         min(t / 100, 1) if t else 0),
        ("vram", "", f"{used:.1f}" if used is not None else "--",
         f"/{total:.0f} GB" if total else "GB", NEON, used / total if total else 0),
        ("ram", f"{mts} MT/s" if mts else "", f"{ram:.0f}" if ram is not None else "--", "%", NEON, ram / 100 if ram else 0),
    ]


# --- drawing ----------------------------------------------------------------------
FONT_CANDIDATES = [
    "/usr/share/fonts/google-noto-vf/NotoSansMono[wght].ttf",                  # Fedora
    "/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf",                    # Debian / Ubuntu
    "/usr/share/fonts/noto/NotoSansMono-Bold.ttf",                             # Arch
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",         # Fedora
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",                # Debian / Ubuntu
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",                            # Arch
]
FONT_FILE = FONT if FONT != "auto" else next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)


def font(size, weight="Bold"):
    if not FONT_FILE:
        return ImageFont.load_default(size)
    f = ImageFont.truetype(FONT_FILE, size)
    try:
        f.set_variation_by_name(weight)       # variable fonts only
    except (OSError, ValueError):
        pass
    return f


F_LABEL, F_VALUE, F_UNIT, F_SMALL = font(18), font(29), font(18), font(9, "Regular")
F_BAR = font(10)
HEX = "0123456789ABCDEF"


def load_background():
    try:
        return Image.open(BACKGROUND).convert("RGB").resize((CW, CH))
    except OSError as e:
        log(f"no background ({e}), using black")
        return Image.new("RGB", (CW, CH))


class Rain:
    """Falling hex columns, moved a little every frame."""
    STEP = 12

    def __init__(self):
        self.cols = [self._new(x, start=True) for x in range(4, CW, self.STEP)]

    def _new(self, x, start=False):
        n = random.randint(4, 12)
        return {"x": x, "y": random.uniform(-CH, CH) if start else -n * self.STEP,
                "v": random.uniform(1.5, 4.5), "n": n,
                "c": [random.choice(HEX) for _ in range(n)]}

    def draw(self):
        layer = Image.new("RGB", (CW, CH))
        d = ImageDraw.Draw(layer)
        for i, c in enumerate(self.cols):
            c["y"] += c["v"]
            if c["y"] > CH:
                c = self.cols[i] = self._new(c["x"])
            if random.random() < 0.3:                          # characters flicker
                c["c"][random.randrange(c["n"])] = random.choice(HEX)
            for k, ch in enumerate(c["c"]):
                y = c["y"] + k * self.STEP
                if 0 <= y < CH:
                    a = int(40 + 140 * k / c["n"])
                    d.text((c["x"], y), ch, font=F_SMALL,
                           fill=(180, 255, 200) if k == c["n"] - 1 else (0, a, a // 3))
        return layer.point(lambda v: int(v * RAIN))


def stats_overlay(rows):
    """Text layer (RGBA) with a dark outline for readability and a soft green glow."""
    txt = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    pitch = (CH - 2 * EDGE) // len(rows)               # 18 px label, 29 px number, bar: ~56 px per row
    for i, (label, extra, value, unit, color, bar) in enumerate(rows):
        y = EDGE + i * pitch - 7                       # -7: empty space above the glyphs
        x = EDGE
        head = f"> {label}_"
        d.text((x, y), head, font=F_LABEL, fill=LABEL)
        if extra:                                    # e.g. voltage / RAM speed beside the label
            d.text((x + d.textlength(head + " ", font=F_LABEL), y), extra, font=F_LABEL, fill=UNIT)
        d.text((x, y + 48), value, font=F_VALUE, fill=color, anchor="ls")
        d.text((x + 3 + d.textlength(value, font=F_VALUE), y + 48), unit, font=F_UNIT, fill=UNIT, anchor="ls")
        n = 14
        full = round(bar * n)
        d.text((x, y + 50), "[" + "#" * full + "." * (n - full) + "]", font=F_BAR, fill=BARC)
    a = txt.getchannel("A")
    shadow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    shadow.putalpha(a.filter(ImageFilter.MaxFilter(5)).point(lambda v: int(v * 0.85)))
    glow = Image.new("RGBA", (CW, CH), (60, 255, 120, 0))
    glow.putalpha(a.filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.7)))
    out = Image.alpha_composite(shadow, glow)
    return Image.alpha_composite(out, txt)


def compose(bg, rain, overlay):
    im = ImageChops.add(bg, rain.draw()) if RAIN else bg.copy()
    im.paste(overlay, (0, 0), overlay)
    if ROTATE:
        im = im.rotate(ROTATE, expand=True)
    r, g, b = im.split()
    return Image.merge("RGB", (b, g, r)).tobytes()                 # panel wants BGR


# --- device -----------------------------------------------------------------------
def find_hidraw():
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        if "0B05:1988" in os.path.realpath(os.path.join(node, "device")).upper():
            return "/dev/" + os.path.basename(node)
    return None


class Ryujin:
    def __init__(self):
        path = find_hidraw()
        if not path:
            raise OSError("hidraw node not found")
        self.usb = usb.core.find(idVendor=VID, idProduct=PID)
        if self.usb is None:
            raise OSError("USB device not found")
        self.fd = os.open(path, os.O_RDWR)
        self.path = path

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            usb.util.dispose_resources(self.usb)
        except (usb.core.USBError, AttributeError):
            pass

    def hid(self, p):
        assert p in ALLOWED, f"refused {p.hex(' ')}"
        while select.select([self.fd], [], [], 0)[0]:          # drop replies, none are needed
            os.read(self.fd, 65)
        os.write(self.fd, b"\xEC" + p.ljust(64, b"\0"))

    def start_live(self):
        self.hid(LIVE)
        time.sleep(0.1)
        self.hid(ANNOUNCE)
        time.sleep(0.1)

    def frame(self, data):
        for off in range(0, FRAME, CHUNK):
            n = self.usb.write(0x01, data[off:off + CHUNK], timeout=1000)   # last one exact
            if n != min(CHUNK, FRAME - off):
                raise OSError(f"short bulk write {n}")
        self.hid(ANNOUNCE)


def restore_animation():
    """Play the stored GIF, or the ROG animation (the screen stays powered while the PC is off)."""
    path = find_hidraw()
    if not path:
        return
    fd = os.open(path, os.O_RDWR)
    try:
        if OFFLINE_GIF_SLOT is None:
            os.write(fd, b"\xEC" + ROG_ANIMATION.ljust(64, b"\0"))
            log("stopped: ROG animation restored")
        else:
            for p in (PLAY_LIST, PLAY_GIF):
                assert p in ALLOWED
                os.write(fd, b"\xEC" + p.ljust(64, b"\0"))
                time.sleep(0.2)                      # replies come within ~10 ms; not read
            log(f"stopped: GIF slot {OFFLINE_GIF_SLOT} playing")
    finally:
        os.close(fd)


def run():
    dev = None
    bg, rain = load_background(), Rain()
    overlay, drawn = None, 0.0
    log(f"gpu: {GPU or 'none found'}, font: {FONT_FILE or 'Pillow default'}, "
        f"on stop: {'ROG animation' if OFFLINE_GIF_SLOT is None else f'GIF slot {OFFLINE_GIF_SLOT}'}")
    while True:
        try:
            if dev is None:
                dev = Ryujin()
                dev.start_live()
                log(f"opened {dev.path}, live mode, {FPS} fps, rotate {ROTATE}")
            t0 = time.monotonic()
            if t0 - drawn >= 1.0:                     # new numbers once a second
                overlay, drawn = stats_overlay(stats()), t0
            dev.frame(compose(bg, rain, overlay))     # every frame: the code keeps falling
            time.sleep(max(0.0, 1 / FPS - (time.monotonic() - t0)))
        except (OSError, usb.core.USBError) as e:
            log(f"device error: {e}; retrying in 5 s")
            if dev is not None:
                dev.close()
            dev = None
            time.sleep(5)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        run()
    except KeyboardInterrupt:                        # Ctrl+C when run by hand
        pass
    finally:
        try:
            restore_animation()
        except OSError as e:
            log(f"stopped: could not restore the animation: {e}")
