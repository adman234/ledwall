# Building the wall

Physical, electrical and mechanical notes for a 50 × 100 (5,000 pixel) RGBW
wall on a rollable 4 ft × 8 ft substrate.

---

## 1. The pitch problem — read this before ordering strip

A 4 ft × 8 ft sheet is 1219 × 2438 mm. A 50 × 100 grid has the same 1:2 ratio,
so *square* pixels filling the sheet exactly would need a pitch of

    1219 / 50 = 24.4 mm        2438 / 100 = 24.4 mm

24.4 mm is about **41 LEDs/m**, and nobody stocks that. LED pitch along a strip
is fixed by the strip you buy; only the *row spacing* is yours to choose. So
"50 × 100" and "fills a 4 × 8 sheet" cannot both be true with off-the-shelf
strip. Here is what each stocked density actually gives you:

| Density | Pitch | 100 px along a row | 50 rows, square pixels |
|--------:|------:|-------------------:|-----------------------:|
| 30/m  | 33.3 mm | 3333 mm (10.9 ft) — longer than the sheet | 1667 mm (65.6") |
| 60/m  | 16.7 mm | 1667 mm (65.6") | 833 mm (32.8") |
| 96/m  | 10.4 mm | 1042 mm (41.0") | 521 mm (20.5") |
| 144/m | 6.9 mm  | 694 mm (27.3")  | 347 mm (13.7") |

### Your four realistic options

| | Strip | Grid | Lit area | Pixels | `pixel_aspect` |
|-|-------|------|----------|-------:|---------------:|
| **A** | 60/m, rows on 16.7 mm centres | 50 × 100 | 833 × 1667 mm (32.8 × 65.6") | 5,000 | `1.0` |
| **B** | 60/m, rows on 24.4 mm centres | 50 × 100 | 1219 × 1667 mm (48 × 65.6") | 5,000 | `0.683` |
| **C** | 30/m, rows on 33.3 mm centres | 36 × 73 | 1200 × 2433 mm (full sheet) | 2,628 | `1.0` |
| **D** | 60/m filling the sheet | 73 × 146 | full sheet | 10,658 | `1.0` |

* **A** — square pixels, exactly the grid you asked for. The lit area is
  smaller than the sheet: centre it and you get a 7.6" border down each side
  and 15.2" at each end, or trim the substrate to about 36 × 70" and save the
  weight. Simplest to wire and to reason about.
* **B** — *recommended if you want it to feel like a 4 × 8 wall.* Same 5,000
  pixels and the same strip, but the rows are spread out to fill the full 4 ft
  width. Pixels end up taller than they are wide, so set
  `grid.pixel_aspect: 0.683` and ledwall crops the camera to match — people
  come out correctly proportioned rather than squashed. The leftover 30" of
  the 8 ft length is a convenient home for the roller core, PSUs and
  controllers.
* **C** — if you would rather fill the sheet than hit 50 × 100. Half the
  pixels, half the power, half the data, much easier first build. Set
  `grid: {width: 73, height: 36}`.
* **D** — don't. 10,658 RGBW pixels is ~2.6 kW theoretical and 42 Mbit/s of
  DDP at 30 fps.

Everything in the software is resolution-agnostic, so this is purely a
hardware decision — change `grid:` and `wiring:` and the rest follows.

---

## 2. Strip choice

**WS2814, 12 V, RGBW, 60 LEDs/m, IP30 or IP65.**

Why 12 V rather than the 5 V SK6812:

* 5,000 SK6812 RGBW pixels at 5 V is ~300 A at full white. That is not a
  wiring job you want. The same wall at 12 V draws ~100 A.
* WS2814 carries a **backup data line** (like WS2815). One dead LED does not
  kill the rest of the run. At 5,000 pixels and thousands of solder joints
  this is the single most valuable feature on the strip.

⚠️ **Check before you buy:** some 12 V RGBW strips control LEDs in groups of
three. A "60/m" strip like that gives you 20 *controllable* pixels per metre,
not 60. Confirm with the seller that it is individually addressable —
per-pixel cut marks, not one cut mark every three LEDs.

Quantity for options A/B: 5,000 px ÷ 60 per m = **83.4 m**, so order ~90 m
(18 × 5 m reels) to cover cutting waste and mistakes.

---

## 3. Power

Per LED at full white, all four channels: 12 V RGBW 60/m is typically rated
14.4 W/m → **0.24 W (20 mA at 12 V) per pixel**.

| | Power | Current @ 12 V |
|-|------:|---------------:|
| Theoretical maximum (all 5,000 white, brightness 1.0) | 1,200 W | 100 A |
| Worst case at the shipped 35 % cap (white test pattern) | 420 W | 35 A |
| Typical person-mask content (~25 % of pixels lit) | ~105 W | ~9 A |

**Do not size the supply for 1,200 W.** Size it for the cap you actually
enforce, and then enforce it — in WLED's Automatic Brightness Limiter *and* in
`output.brightness`.

Recommended: **2 × 12 V 350 W (29 A)** supplies, one per controller zone
group, giving 700 W total. That runs the 35 % cap with real headroom.

### Distribution and injection

* Each row is 1.667 m of strip drawing up to 2 A. Strip copper cannot carry
  that end-to-end without a visible red shift, so **inject at both ends of
  every row**.
* Run a **14 AWG silicone bus** down each vertical edge of the sheet, and tap
  each row end with 18–20 AWG.
* Split the bus into the same 8 zones as the data outputs (~625 px each).
  Each zone can draw up to 12.5 A unclamped → **14 AWG feed and a 15 A fuse
  per zone.** Fuse at the supply, not at the strip.
* Tie **all grounds together** — every PSU, every controller, the Pi. Shared
  ground is what makes the data signal work.
* Never power the strip from the Pi or the ESP32.

---

## 4. Data: why eight outputs, not one

WS2814 is a 32-bit-per-pixel protocol at 800 kHz, so each LED costs **40 µs**
to clock out:

| LEDs on one output | Time per refresh | Ceiling |
|-------------------:|-----------------:|--------:|
| 5,000 | 200 ms | 5 fps |
| 2,500 | 100 ms | 10 fps |
| 625   | 25 ms  | **40 fps** |

WLED on an ESP32 drives its outputs in parallel, so the ceiling is set by the
*longest* output, not the total. **625 pixels per output** is the number that
makes 30 fps comfortable.

That means 8 outputs — either 2 × 4-output controllers (the shipped config) or
one 8-output board.

### Network

| | |
|-|-|
| Frame | 5,000 px × 4 bytes = **20,000 bytes** |
| At 30 fps | 600 kB/s = **4.8 Mbit/s** total |
| Per controller | 2.4 Mbit/s, 7 DDP packets per frame, ~210 packets/s |

**Use Ethernet, not Wi-Fi.** An ESP32 on Wi-Fi drops packets at this rate, and
a dropped DDP packet is a visible band of stale pixels for that frame. Pick
controllers with an Ethernet port (the QuinLED Dig-Octa / Dig-Quad boards with
the Ethernet option are the usual choice; several Gledopto units have it too —
check the specific model, as the Wi-Fi-only variants look identical).

The Pi 3B+ has 100 Mbit-class networking shared with the USB bus. 4.8 Mbit/s
of DDP plus an MJPEG webcam is well within it.

---

## 5. Making it roll

**Strips must run parallel to the long axis, and you roll along that same
axis.** LED strip bends happily along its length and not at all across its
width. Roll the 8 ft direction up around a core running parallel to the 4 ft
edge and every strip bends the correct way.

* **Core:** 150 mm (6") tube. Minimum bend radius for silicone-sleeved strip
  is ~40–50 mm; a 75 mm radius core is safe. Rolled up, the whole thing is
  about 170 mm diameter and 4 ft long.
* **Substrate:** coated banner PVC, 1000D coated polyester, or 2–3 mm EVA
  foam. It needs to be dimensionally stable when hung and floppy in one axis.
* **Attachment:** strip adhesive alone *will* let go after a few roll cycles.
  Use 3M VHB plus mechanical retention (zip ties or stitching through the
  substrate) every ~300 mm.
* **The joints are the failure point.** Row-to-row jumpers should be
  *flexible silicone wire* with a small service loop, never bare rigid
  bridges. Stagger jumper positions between adjacent rows so you do not build
  a stiff ridge down one edge that fights the roll. Cover each joint with
  adhesive-lined heatshrink or a bead of neutral-cure silicone.
* **Roll it lit-side out** — the strips are then on the outside of the curve
  in tension rather than compressed and buckling.

### Diffusion

At 16.7–24.4 mm pitch you need roughly 20–25 mm of standoff behind a diffuser
for the pixels to blend, and a standoff does not roll. Options, in order of
practicality:

1. **No diffuser.** Bare pixels, which is what most builds of this type look
   like anyway. Simplest, and it rolls.
2. A **separate** rolled diffuser (white ripstop or PEVA shower curtain) hung
   on standoffs in front when deployed.
3. Rigid diffuser panels — good-looking, but you have given up on rolling.

---

## 6. Camera

Any UVC webcam. Mount it centred on the wall facing the viewer, ideally at
head height; a wide-FOV lens helps in a small room. Prefer a camera that
supports MJPG at 640 × 480 — on a Pi 3B+ raw YUYV eats both USB bandwidth and
CPU (`camera.fourcc: MJPG`, which is the default).

If the wall is a mirror-image installation (viewer stands in front of it),
leave `camera.mirror: true` so moving left moves the image left.

---

## 7. Bill of materials (option A or B)

| Item | Qty | Notes |
|------|----:|-------|
| WS2814 12 V RGBW 60/m strip | ~90 m | individually addressable — verify |
| 12 V 350 W PSU | 2 | 29 A each |
| 4-output Ethernet WLED controller | 2 | or 1 × 8-output |
| Raspberry Pi 3B+ (64-bit Pi OS) | 1 | + 16 GB+ card, 2.5 A supply |
| USB webcam (MJPG capable) | 1 | |
| 14 AWG silicone wire | ~20 m | power buses |
| 18–20 AWG silicone wire | ~40 m | row taps and data jumpers |
| Inline fuse holders + 15 A fuses | 8 | one per zone |
| 4 × 8 ft flexible substrate | 1 | banner PVC / EVA foam |
| 150 mm tube | 1 | roller core |
| 3M VHB tape, zip ties, heatshrink | — | |
| Network switch + cable | 1 | Pi and both controllers wired |

---

## 8. Build order that saves pain

1. Wire and test **one row** on the bench end to end before cutting 49 more.
2. Build **one output** (6–7 rows) and run `ledwall map --by line` on it.
3. Only then commit to the full wall.
4. Solder every joint on a bench, not on the hanging sheet.
5. Run `ledwall map --by output` after each output is added — catching a
   reversed serpentine at output 3 is cheap, at output 8 it is not.

Bad solder joints are the number one failure in builds like this, and a
5,000-pixel wall has roughly 200 hand joints even with jumpers. Budget time
for it, and use the backup-data-line strip so a single failure does not
black out a whole run.
