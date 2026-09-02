# ledwall

Camera-driven person tracking onto a large WLED matrix, over DDP, from a
headless Raspberry Pi.

A webcam sees you, a segmentation model cuts your silhouette out of the frame,
and that silhouette is painted onto the wall in colour at 30 fps. Built for a
**50 × 100 (5,000 pixel) RGBW wall on a rollable 4 × 8 ft sheet**, but the
grid size, wiring order and controller layout are all config.

Everything runs on the Pi with no screen attached — there is no OpenCV preview
window, just a web page you open from your laptop.

```
webcam ──► Pi 3B+ ──────────────────────────────► WLED controllers ──► 5,000 RGBW pixels
           capture → segment → render → DDP/UDP        (8 outputs, 625 px each)
                       │
                       └──► http://<pi>:8080  status + live preview + controls
```

## What's here

| | |
|---|---|
| [docs/BUILD.md](docs/BUILD.md) | Physical build: **pitch maths, strip choice, power budget, wiring, how to make it roll** |
| [docs/WLED.md](docs/WLED.md) | Per-controller WLED configuration and how to verify it |
| [docs/TUNING.md](docs/TUNING.md) | Making a Pi 3B+ hold 30 fps |

**Read [docs/BUILD.md §1](docs/BUILD.md#1-the-pitch-problem--read-this-before-ordering-strip) before you order strip.** A 50 × 100 grid
filling a 4 × 8 sheet needs a 24.4 mm pitch (~41 LEDs/m), which does not exist.
There are four sensible ways out and that section lays them out.

## Install

On a Raspberry Pi running **64-bit** Raspberry Pi OS (Bookworm recommended):

```bash
git clone https://github.com/adman234/ledwall.git && cd ledwall
sudo ./scripts/install.sh
```

That creates a venv in `/opt/ledwall`, installs a segmentation runtime
appropriate to the architecture, downloads the model, writes
`/etc/ledwall/ledwall.yaml`, and installs a systemd unit.

Add `--with-mediapipe` for the best mask quality (64-bit only; the installer
repairs the headless-OpenCV conflict mediapipe introduces).

Then:

```bash
sudo nano /etc/ledwall/ledwall.yaml   # 1. set the controller IPs
ledwall doctor                        # 2. check deps, config, controllers, camera
ledwall buses                         # 3. numbers to type into WLED
ledwall map --by output               # 4. verify the wiring on the actual wall
sudo systemctl start ledwall          # 5. go
```

Web preview: `http://<pi-address>:8080`

### Development install

```bash
python3 -m venv venv && ./venv/bin/pip install -e '.[dev]'
./venv/bin/python -m pytest
```

## Commands

| Command | What it does |
|---|---|
| `ledwall run` | The main loop. `--source pattern` needs no camera. |
| `ledwall doctor` | Checks deps, config, WLED reachability + LED counts, and cameras. Start here when something is wrong. |
| `ledwall buses` | Prints the LED-settings table to type into each WLED device. |
| `ledwall test <pattern>` | Sends a test pattern with no camera involved: `corner`, `border`, `gradient`, `rows`, `columns`, `checker`, `chase`, `solid`, `index`. |
| `ledwall map --by output` | Lights one output at a time, in order, naming it as it goes. |
| `ledwall map --by line` | One strip run at a time, with a red pip marking the left edge. |
| `ledwall bench [--segment]` | Measures render, wiring and model speed on *this* machine. |
| `ledwall init [path]` | Writes a starter config. |
| `ledwall cameras` | Lists video devices that actually deliver frames. |

`map` and `test` are the ones that save you. A 5,000-pixel wall will have a
reversed serpentine or a swapped output somewhere, and finding it by staring
at a person-shaped blob is miserable.

## Configuration

One YAML file drives everything — see
[`config/wall-50x100.yaml`](config/wall-50x100.yaml) for the annotated
default. The parts that matter:

```yaml
grid:
  width: 100
  height: 50
  pixel_aspect: 1.0     # horizontal pitch / vertical pitch; < 1 if rows are
                        # spaced wider than the LEDs along the strip

wiring:
  orientation: rows     # strips run along the long axis
  start_corner: top-left
  serpentine: true
  serpentine_scope: output    # each output restarts at the same edge
  controllers:
    - name: wall-a
      host: 192.168.1.51
      lines: [0, 25]    # this controller owns strip runs 0..24
      outputs: 4        # split evenly -> 7,6,6,6 runs
```

The config is validated on load: overlapping controllers, unassigned strip
runs, output splits that don't add up, and unknown keys are all rejected with
a message naming the problem. The resulting map is then checked to be exactly
one-to-one — every grid pixel driving exactly one LED — before anything is
sent.

`source.kind` picks what drives the mask: `person` (segmentation), `motion`
(background subtraction, static camera), or `pattern` (an animated blob, so
you can commission the wall before the camera works).

## How it works

**Geometry** (`geometry.py`) turns the wiring config into, per controller, a
numpy gather array: `take[i]` is the flat grid index feeding LED *i*. Sending
a frame is then one fancy-index per controller, which is why the mapping costs
~0.1 ms for 5,000 pixels instead of a Python loop.

**DDP** (`ddp.py`) speaks the wire format WLED actually parses — 10-byte
header, `0x1B` for RGBW, offsets in channels, payloads capped at 1440 bytes,
and the push flag set only on a frame's final packet so WLED renders once per
frame rather than seven times. Verified against WLED's `handleDDPPacket`.

**Rendering** (`render.py`) works in linear float, splits RGB into RGBW
(`accurate` mode moves the achromatic component onto the white die, which
keeps hues and is far more efficient than making white from three coloured
dies), then applies brightness and gamma last.

**Rates** are decoupled — capture, inference and output all run at their own
speed. See [docs/TUNING.md](docs/TUNING.md).

## Status

The DDP output path, geometry mapping, renderer, config validation, pipeline
and web API are covered by tests, including an end-to-end test that runs the
pipeline against a fake WLED receiver which reassembles DDP frames the way
WLED does and asserts the right physical LED lights up.

The camera and segmentation backends are **not** hardware-tested — there was
no camera or Pi in the loop while writing this. `ledwall doctor` and
`ledwall bench --segment` exist to tell you quickly whether they work on
yours.

## Licence

MIT.
