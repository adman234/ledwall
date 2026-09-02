# Getting frames out of a Pi 3B+

The Pi 3B+ is the constraint in this build: four Cortex-A53 cores at 1.4 GHz,
no useful GPU compute for this, and USB sharing the network bus. It is enough,
but only because the pipeline is arranged to keep it that way.

## The three rates

```
capture thread     as fast as the webcam delivers, newest frame only
inference thread   source.infer_fps   (~10-12 on a 3B+)
output loop        output.fps         (30)
```

Segmentation is the expensive step by an order of magnitude. Rather than
letting it set the wall's frame rate, it runs slower and the output loop eases
toward each new mask (`source.smoothing`). The wall updates at 30 fps and the
motion reads as smooth even though the person mask underneath is refreshing at
12 — the same trick a game uses to decouple rendering from physics.

Measure what your Pi actually does:

```bash
ledwall bench --segment
```

For reference, the render + wiring-map step for 5,000 pixels measures about
**0.8 ms/frame on x86-64**; expect roughly 5–12 ms on a Pi 3B+, against a
33 ms budget at 30 fps. That part is not your bottleneck. Segmentation is:
budget 60–120 ms per inference for the 256×256 selfie segmenter on this CPU,
i.e. 8–15 fps. Measure rather than assume — it varies with the backend and
with what else is running.

## Knobs, roughly in order of effect

| Setting | Effect |
|---|---|
| `source.infer_fps` | The big one. Lower it until the CPU has headroom. |
| `source.infer_size` | Frames are downscaled to this before inference. 256 is the model's native size; 192 is noticeably faster and slightly coarser. |
| `camera.width/height` | 640×480 is plenty. Capturing 1080p to throw it away is pure waste. |
| `camera.fourcc: MJPG` | Raw YUYV over USB costs both bandwidth and CPU. Keep MJPG. |
| `output.fps` | 30 looks good; 25 buys headroom cheaply. |
| `source.smoothing` | Higher hides a low `infer_fps` better, at the cost of lag. |
| `web.enabled` | The MJPEG preview costs real CPU. Turn it off once the wall works. |

## Backends

| Backend | Needs | Notes |
|---|---|---|
| `mediapipe` | 64-bit OS (`manylinux_2_28_aarch64` wheels; no 32-bit build) | Best masks. Pulls in `opencv-contrib-python`, the GUI build — the installer force-reinstalls the headless variant afterwards, otherwise `import cv2` fails on a headless Pi with `libGL.so.1: cannot open shared object file`. |
| `tflite` | `ai-edge-litert` (aarch64) or `tflite-runtime` (aarch64 **and** armv7l, glibc ≥ 2.34 so Bookworm) | Same model, much lighter install. The installer's default. |
| `mog2` | nothing beyond OpenCV | No ML. Segments *movement*, so it needs a static camera and a few seconds to learn the background. Always available, and a good way to prove the wall works before fighting with model installs. |

`source.backend: auto` tries mediapipe → tflite → mog2 and uses the first that
loads. `ledwall doctor` shows which are importable.

**Use 64-bit Raspberry Pi OS (Bookworm).** On 32-bit you lose mediapipe
entirely and `ai-edge-litert`, leaving `tflite-runtime` or `mog2`.

## If frames are stuttering

1. `ledwall doctor` — check for controller errors and LED-count mismatches.
2. Watch the web preview's *Output fps* vs *Inference fps*. If output fps is
   below `output.fps`, the Pi is the problem; if it is at target but the wall
   looks choppy, the network or the controllers are.
3. Check `errors` per controller in the web UI. Non-zero means the kernel send
   buffer filled — usually a Wi-Fi controller that cannot keep up.
4. `vcgencmd get_throttled` — a non-zero result means undervolt or thermal
   throttling. A Pi 3B+ under sustained load on a weak supply will throttle,
   and the symptom looks exactly like slow segmentation.
5. `top` — if `ledwall` is pinned at ~400 % CPU, lower `infer_fps` first.
