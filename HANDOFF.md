# ledwall — project handoff

Everything needed to pick this project up cold, in a new repo or a new
session. Written for whoever (or whatever) continues the work.

**What it is:** a webcam watches a room, a segmentation model cuts the
person's silhouette out of the frame, and that silhouette is painted onto a
large RGBW LED matrix at 30 fps. Runs headless on a Raspberry Pi 3B+, drives
WLED controllers over DDP/UDP.

**Target build:** 50 × 100 = 5,000 RGBW pixels on a rollable 4 × 8 ft sheet,
two 4-output WLED controllers, Raspberry Pi 3B+.

Inspired by Chris Maher's 50×50 grid build; this is the scaled-up,
Pi-hosted, headless version of that idea.

---

## 1. Status

| Area | State |
|---|---|
| DDP output | Written and tested against a fake WLED receiver |
| Geometry / wiring map | Written and tested (13 cases) |
| Renderer, palettes, RGBW split | Written and tested |
| Config validation | Written and tested |
| Pipeline + web API | Written, end-to-end tested |
| CLI (`run/doctor/buses/test/map/bench/init/cameras`) | Written, smoke-tested |
| Pi installer + systemd unit | Written, **not run on real hardware** |
| Camera capture | Written, **never run against a real camera** |
| Segmentation backends | Written, **never run against a real model** |

**35 tests pass.** Includes an end-to-end test that runs the pipeline against
a fake WLED receiver which reassembles DDP frames the way WLED does, and
asserts the correct physical LED lights up.

The honest gap: no Pi, no camera and no LED hardware were available while
writing this. Everything that could be verified in software was; the camera
and model paths are written carefully but are unproven. `ledwall doctor` and
`ledwall bench --segment` exist specifically to shorten that first bring-up.

---

## 2. Hardware decisions (these drove the software)

### 2.1 The pitch problem — the single most important finding

A 4 × 8 ft sheet is 1219 × 2438 mm. A 50 × 100 grid is the same 1:2 ratio, so
square pixels filling it exactly need a pitch of **1219 ÷ 50 = 24.4 mm** —
about 41 LEDs/m. **That density does not exist.**

Pitch *along* a strip is fixed by the strip you buy; only *row spacing* is
free. So "50 × 100" and "fills a 4 × 8 sheet" cannot both be true with
off-the-shelf strip. The four ways out:

| | Strip | Grid | Lit area | Pixels | `pixel_aspect` |
|-|-------|------|----------|-------:|---------------:|
| A | 60/m, rows on 16.7 mm | 50 × 100 | 833 × 1667 mm (32.8 × 65.6″) | 5,000 | `1.0` |
| **B** | 60/m, rows on 24.4 mm | 50 × 100 | 1219 × 1667 mm (48 × 65.6″) | 5,000 | `0.683` |
| C | 30/m, rows on 33.3 mm | 36 × 73 | full sheet | 2,628 | `1.0` |
| D | 60/m filling the sheet | 73 × 146 | full sheet | 10,658 | — |

**B is the recommendation** — fills the full 4 ft width with the same 5,000
pixels. Its pixels are taller than wide, which is why `grid.pixel_aspect`
exists: it makes the camera crop match the wall's *physical* aspect so people
aren't vertically squashed. D is ~2.6 kW and 42 Mbit/s — don't.

### 2.2 Strip: WS2814, 12 V, RGBW, 60/m

* 5,000 SK6812 RGBW at **5 V is ~300 A** at full white. At 12 V it's ~100 A.
* WS2814 has a **backup data line** — one dead LED doesn't kill the run. At
  5,000 pixels and ~200 hand-soldered joints this matters more than anything.
* ⚠️ Some 12 V RGBW strips address LEDs **in groups of three**. A "60/m" strip
  like that gives 20 controllable pixels/m, not 60. Verify before buying.

### 2.3 Power

0.24 W (20 mA @ 12 V) per pixel at full white, from a 14.4 W/m strip rating.

| Condition | Power | Current |
|---|---:|---:|
| Theoretical max (all white, brightness 1.0) | 1,200 W | 100 A |
| Worst case at the shipped 35 % cap | 420 W | 35 A |
| Typical person-mask content | ~105 W | ~9 A |

Size for the cap you enforce, not the theoretical max: 2 × 12 V 350 W.
Inject both ends of every row, 14 AWG bus per edge, 8 zones, 15 A fuse each,
all grounds tied together.

### 2.4 Why eight outputs, not one

RGBW is 32 bits/pixel at 800 kHz = **40 µs per LED**, serial along a chain:

| LEDs on one output | Refresh | Ceiling |
|---:|---:|---:|
| 5,000 | 200 ms | 5 fps |
| 2,500 | 100 ms | 10 fps |
| 625 | 25 ms | **40 fps** |

WLED drives outputs in parallel, so the ceiling is set by the *longest* chain.
8 × 625 px is what makes 30 fps comfortable.

Network: 20,000 bytes/frame, 4.8 Mbit/s at 30 fps, 7 DDP packets per frame per
controller. **Use Ethernet** — an ESP32 on Wi-Fi drops packets at this rate and
a dropped DDP packet is a visible band of stale pixels.

### 2.5 Rolling

LED strip bends along its length, not across its width. So **strips must run
parallel to the long axis and you roll along that same axis** — 150 mm core,
strips run along the 8 ft direction, roll the 8 ft direction up. Roll lit-side
out. Flexible silicone jumpers with service loops; stagger them between rows.
Strip adhesive alone fails after a few roll cycles — add mechanical retention
every ~300 mm.

---

## 3. Verified external facts

Researched during the build; don't re-derive these.

**DDP wire format** — read from WLED source, not guessed. Receiver is
`wled00/e131.cpp` → `handleDDPPacket`; constants in
`wled00/src/dependencies/e131/ESPAsyncE131.h`:

```
port 4048, 10-byte header
byte 0    flags: 0x40 = version 1, |0x01 = PUSH ("render now")
byte 1    sequence, low 4 bits (0 = unused)
byte 2    data type: 0x0B = RGB 8bpc, 0x1B = RGBW 8bpc
byte 3    destination: 1 = display
byte 4-7  channel offset, uint32 big-endian, counted in CHANNELS (bytes)
byte 8-9  data length, uint16 big-endian, in bytes
byte 10+  pixel data
```

Two details that bite:
* WLED detects RGBW as `(dataType & 0b00111000) >> 3 == 0b011`. It must also
  have an RGBW **bus type** configured or the data is misinterpreted.
* WLED renders on the PUSH flag. Set it **only on a frame's last packet**, or
  it renders 7× per frame.
* WLED rejects a packet whose declared length exceeds what arrived — hence the
  1440-byte payload cap (divisible by both 3 and 4).

**Segmentation runtime availability on Raspberry Pi:**

| Package | Wheels | Implication |
|---|---|---|
| `mediapipe` 1.0.1 | `manylinux_2_28_aarch64`, no armv7l | 64-bit Pi OS only |
| `ai-edge-litert` 2.2.0 | `manylinux_2_27_aarch64` only | 64-bit only, works on Bullseye |
| `tflite-runtime` 2.14.0 | `manylinux_2_34` aarch64 **and armv7l** | only ML option on 32-bit; needs Bookworm (glibc ≥ 2.34) |

`mediapipe` depends on **`opencv-contrib-python`** — the GUI build. It replaces
headless OpenCV and then fails on a headless Pi with
`libGL.so.1: cannot open shared object file`. The installer force-reinstalls
`opencv-contrib-python-headless` afterwards to repair this.

**Model:** `selfie_segmenter.tflite`, 256×256 input, from
`https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/1/selfie_segmenter.tflite`
(verified live). The TFLite backend introspects tensor shapes at runtime and
handles both 1-channel sigmoid and 2-channel softmax outputs.

---

## 4. Code map

```
ledwall/
  config.py        YAML -> validated dataclasses. Rejects overlapping
                   controllers, unassigned strip runs, bad output splits,
                   unknown keys — each with a message naming the problem.
  geometry.py      Wiring -> per-controller numpy gather array `take`, where
                   take[i] is the flat grid index feeding LED i. Verifies the
                   map is exactly one-to-one before anything is sent.
                   Also emits the WLED bus table.
  ddp.py           DDPSender (one device) + WallSender (fan-out). Preallocated
                   packet buffer, non-blocking socket, drops rather than
                   stalling when the kernel send buffer fills.
  render.py        Mask -> uint8 frame. Linear float -> RGBW split ->
                   brightness -> gamma (in that order; gamma last).
  palettes.py      Gradient stops baked into 256-entry LUTs.
  patterns.py      Test frames that encode position (corner/border/gradient/
                   rows/columns/chase/...) for wiring verification.
  camera.py        Threaded capture, newest-frame-only, auto-reopen on USB
                   dropout. v4l2 / picamera2 / any backends.
  segmentation.py  mediapipe | tflite | mog2, common interface, auto-fallback.
  sources.py       Mask sources: SegmentSource, PatternSource. fit_to_grid
                   handles cover/contain/stretch and non-square pixels.
  pipeline.py      Three decoupled rates (capture / infer / output) + stats.
  web.py           Stdlib HTTP: status page, MJPEG previews, JSON control API.
  cli.py           All subcommands.
config/wall-50x100.yaml   Annotated default config
scripts/install.sh        Pi installer (arch detection, backend selection)
scripts/ledwall.service   systemd unit
docs/BUILD.md             Physical build: pitch, power, wiring, rolling
docs/WLED.md              Per-controller WLED setup + verification
docs/TUNING.md            Pi 3B+ performance
tests/                    35 tests
```

### Key design choices worth preserving

**Gather arrays, not loops.** The wiring config compiles once into
`take[i] = flat grid index`. Sending a frame is one numpy fancy-index per
controller — ~0.1 ms for 5,000 px instead of a Python loop.

**Three decoupled rates.** Segmentation is ~10× more expensive than everything
else. Rather than letting it set the wall's frame rate, it runs slower
(`source.infer_fps`, ~12) while the output loop runs at 30 and eases toward
each new mask (`source.smoothing`). This is what makes a 3B+ viable.

**Gamma last.** Colour maths in linear float, then the RGB→RGBW split, then
brightness, then gamma. Gamma-correcting before the white split skews hues.
`white_mode: accurate` moves the achromatic component onto the white die and
subtracts it from RGB — preserves hue and is far more efficient than making
white from three coloured dies.

**Headless by design.** No `cv2.imshow`. The original inspiration ran on a
Mac with a preview window; this runs on a headless Pi, so status, live preview
and controls are served over HTTP from the stdlib (no Flask).

---

## 5. Config schema

```yaml
grid:
  width: 100
  height: 50
  pixel_aspect: 1.0        # horizontal pitch / vertical pitch (see §2.1)

output:
  fps: 30
  brightness: 0.35         # master cap — 5,000 RGBW at 1.0 is ~1.2 kW
  gamma: 2.2
  rgbw: true
  white_mode: accurate     # none | min | accurate
  white_gain: 1.0

wiring:
  orientation: rows        # rows | columns ("lines" = one strip run)
  start_corner: top-left   # where run 0 begins and which way it travels
  serpentine: true
  serpentine_scope: output # output | wall
  controllers:
    - name: wall-a
      host: 192.168.1.51
      port: 4048
      lines: [0, 25]       # [start, end) — this controller owns runs 0..24
      outputs: 4           # int (split evenly) or list of per-output counts

camera:   { device: 0, width: 640, height: 480, fps: 30, fourcc: MJPG,
            backend: auto, mirror: true, rotate: 0 }
source:   { kind: person, backend: auto, model_path: "", infer_fps: 12,
            infer_size: 256, threshold: 0.5, smoothing: 0.5, fit: cover }
render:   { palette: aurora, mode: fill, cycle_seconds: 24, trail: 0.0,
            background: dim, background_level: 0.04 }
web:      { enabled: true, host: 0.0.0.0, port: 8080,
            preview_scale: 6, jpeg_quality: 70 }
```

`source.kind`: `person` (segmentation) | `motion` (background subtraction,
static camera) | `pattern` (animated blob, no camera — commission the wall
before the camera works).

---

## 6. Bring-up order

```bash
sudo ./scripts/install.sh          # venv in /opt/ledwall, model, systemd unit
sudo nano /etc/ledwall/ledwall.yaml   # set controller IPs
ledwall doctor                     # deps, config, WLED reachability + LED counts, cameras
ledwall buses                      # numbers to type into each WLED device
ledwall test corner                # lights only the true top-left 5x5
ledwall map --by output            # one output at a time, named as it goes
sudo systemctl start ledwall
```

`map` and `test` are the ones that save real time — a 5,000-pixel wall *will*
have a reversed serpentine or a swapped output somewhere, and finding it by
staring at a person-shaped blob is miserable.

| Symptom | Cause |
|---|---|
| Colours wrong, positions right | WLED bus Color Order, or RGB/RGBW mismatch |
| Every other row reversed | `serpentine` / `serpentine_scope` |
| Mirrored or upside down | `start_corner` |
| One output dark | wrong GPIO, or Start/Count ≠ `ledwall buses` |
| Bands of stale pixels when moving | dropped DDP packets — get off Wi-Fi |
| Far end of a run dims to red | power injection, not data |

---

## 7. Known gaps / next steps

1. **Hardware bring-up.** Camera and segmentation paths are unproven. Run
   `ledwall doctor` and `ledwall bench --segment` on the Pi first.
2. **Pi 3B+ segmentation rate is an estimate.** I predicted 8–15 fps for the
   256×256 selfie segmenter on 4×A53; not measured. Render + wiring measured
   0.8 ms/frame for 5,000 px on x86-64 — expect 5–12 ms on a Pi, against a
   33 ms budget, so that part is not the bottleneck.
3. **`ledwall map --by output` is untested against real hardware** — the
   payload construction is unit-tested but the visual result isn't.
4. **No multi-person handling.** The selfie segmenter returns one foreground
   mask; two people merge into one blob. Fine for the effect, worth knowing.
5. **No auto-discovery of WLED devices.** IPs are configured by hand; mDNS
   discovery would be a nice addition.
6. **`motion` (MOG2) mode needs a static camera** and a few seconds to learn
   the background. There's a `reset()` but nothing calls it on scene change.
7. Consider an idle/attract mode when nobody has been detected for a while —
   `render.background: idle` is a stub in that direction.

---

## 8. Provenance

Built in a Claude Code session that was, mistakenly, pointed at an unrelated
repo (`adman234/claude-unraid-docker`). That project was never pushed and the
repo has been restored to its original state — this code has no history there.
This is a clean, standalone project.

`docs/BUILD.md` has the long-form version of §2, and there is an illustrated
build guide at
https://claude.ai/code/artifact/0d9be010-0f55-408a-b732-328539fe489e

Licence: MIT.
