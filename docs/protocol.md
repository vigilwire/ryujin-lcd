# Ryujin II LCD protocol notes

Everything here was tested on one **ASUS ROG Ryujin II** (USB `0b05:1988`, firmware
`AURJ1-S750-0104`) on Linux. Other firmware versions may differ.

The cooler has two USB interfaces:

- **Interface 1, HID** (`/dev/hidraw*`): 65-byte reports. Commands start with `EC`, replies with
  `EC <same id>`, events with `EE`.
- **Interface 0, bulk OUT endpoint `0x01`**: picture data (live frames and file uploads). No kernel
  driver is bound to it; pyusb writes to it directly.

## Live frames (what the dashboard uses)

From [zenstrukt/signalrgb-device-plugins](https://github.com/zenstrukt/signalrgb-device-plugins):

```
EC 51 20 00 00            enter live mode
EC 7F 03 00 84 03 00      announce a 230400-byte frame (320 × 240 × 3)
bulk OUT 0x01             the frame as BGR888, in 4096-byte writes; the LAST write must be the exact rest (1024)
EC 7F 03 00 84 03 00      commit this frame + announce the next one
EC 51 14 00 00            back to the ROG animation
```

- If frames stop for a while, the cooler falls back to its stored animation. 10 fps is plenty.
- **While live frames are streamed, the cooler answers no HID request at all**: no `EC D0` status,
  no `EC 99` sensor read (0 of 14 tries, with either frame order). The kernel `rog_ryujin` hwmon
  driver times out too (`Connection timed out`), so coolant temperature and pump/fan RPM cannot be
  read while the dashboard runs. Waiting for replies during streaming causes reconnect loops.
- The panel is 320×240 and mounted sideways in the usual orientation: draw a 240×320 portrait
  picture and rotate it 270° counter-clockwise.

## Stored files: GIF while the PC is off

Sequence from [zaclanzon/ryujin-iii-lcd-linux](https://github.com/zaclanzon/ryujin-iii-lcd-linux)
(Ryujin III, same S750 controller) — works the same on the Ryujin II:

```
EC 71 01 01  (+event EE 12)      refresh the storage table
EC F1                            read the storage table (reply id 71)
EC 72 01 <type> <slot>           select a slot: type 01 = JPG, 02 = GIF
EC 73 01     (+event EE 13 00 01) begin write
EC 7F 02 <size u32 LE>           announce the file size; the reply carries the chunk size
bulk OUT 0x01, chunks            file data, zero-padded; event EE 14 00 00 after each chunk
EC 73 FF     (+event EE 13 00 FF) end write
EC 5D 00 01 10 01 <slot> 05      play list with one entry: the GIF in <slot>
EC 51 10 01 <slot>               play it
```

Ryujin II findings:

| Finding | Detail |
|---|---|
| Storage table layout | 1-byte slot maps: **byte 15 = JPG slots, byte 17 = GIF slots** (bit n = slot n). Uploading three GIFs changed byte 17 `01 → 03 → 07`. Total storage reported: 16040 KB. |
| GIF size | **320×240**. A 640×480 GIF uploads fine but is not shown (falls back to the ROG logo). |
| Interlacing | GIFs must **not** be interlaced. Pillow interlaces by default; the firmware then shows 4 squashed copies. Save with `interlace=False`. |
| PC off | The screen stays powered while the PC is shut down and keeps the last mode. A GIF that plays at shutdown stays on screen while the PC is off (dark only when the PSU has no power). |
| Table while a GIF plays | `EC 71 01 01` sometimes produces only the `EE 12` event and no reply (the tool then waits for the event). |
| After live streaming | The cooler ignores **all** HID commands (table, `51 14`, play) once it has live-streamed, even after the stream stops, until it loses power (full shutdown). Re-authorizing the USB device (`authorized` 0/1) does not help. A real USB port reset was not tested. |
| Replies | `5D` and `51 10` do reply. |

## What these tools never send

- Pump or fan duty commands. On these coolers pump/fan control belongs to the BIOS (or Armoury
  Crate); writing duty over USB can take the pump off the motherboard header. See the warning in
  the ryujin-iii-lcd-linux README.
- `73 03` (delete) or any format/unknown command. Every packet is checked against an allow-list in
  the code before it is sent.

## Dead ends (so nobody repeats them)

- The firmware's own **hardware monitor page** only draws the first line big; lines 2–3 are small,
  whatever theme bytes are sent.
- **Banner mode** (`EC 60` + `5D` + `51 1F` over a stored JPG) never switched on.
