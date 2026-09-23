# WLED controller setup

One-time setup per controller. Do this before running ledwall.

## 1. Flash and network

1. Flash WLED (https://install.wled.me): use an **ESP32** build, not ESP8266.
   An ESP8266 cannot move 2,500 pixels of DDP.
2. Join it to your network. **Prefer Ethernet.** If the board has an Ethernet
   PHY, set *Config → WiFi Setup → Ethernet Type* to match it (e.g. `WT32-ETH01`,
   `QuinLED-Dig-Octa`, `ESP32-POE`).
3. Give each controller a **static IP or DHCP reservation**: ledwall addresses
   them by IP, and a lease change means a dark wall.

## 2. LED outputs

`ledwall buses` prints exactly what to enter:

```
$ ledwall buses
  controller   host             bus   start  count  strip runs
  wall-a       192.168.1.51       0       0    700  0..6
  wall-a       192.168.1.51       1     700    600  7..12
  wall-a       192.168.1.51       2    1300    600  13..18
  wall-a       192.168.1.51       3    1900    600  19..24
  ...
```

In *Config → LED Preferences*, for each output add one bus:

* **Type:** `SK6812 / WS2814 RGBW` (a 4-channel type: this matters, see below)
* **Color Order:** whatever your strip is, usually GRB. Get it right with the
  `ledwall test solid` pattern before you trust anything else.
* **Start** and **Count:** copy from the table above.
* **GPIO:** the pin for that physical output on your board.

Then check the device's **total LED count** matches the controller total
(2,500 in the shipped config). `ledwall doctor` reads this back over WLED's
JSON API and tells you if it disagrees with your config.

### RGBW must match on both sides

ledwall sends DDP data type `0x1B` (RGBW, 4 channels) when `output.rgbw: true`.
WLED only interprets that as 4-channel if its bus type is an RGBW one. If the
bus is configured RGB, the colours will be scrambled and shifted: every pixel
reading one byte off from the last. If you are on RGB strip instead, set
`output.rgbw: false` in your config and ledwall sends `0x0B` (RGB) instead.

## 3. Brightness limiter

*Config → LED Preferences → Automatic Brightness Limiter.*

Enable it and set:

* **mA per LED:** `20` for 12 V WS2814 (see the power section in
  [BUILD.md](BUILD.md)).
* **Max current:** the amperage actually available to that controller's
  share of the wall, with margin: e.g. `15000` mA per controller if you have
  two 29 A supplies feeding four zones each.

WLED's limiter is calibrated around 5 V strip, so on 12 V strip treat it as a
useful safety clamp rather than a precise wattmeter. It is a backstop; the
real limit is `output.brightness` in your ledwall config.

## 4. Realtime behaviour

*Config → Sync Interfaces:*

* **DDP** is enabled by default on UDP port 4048. Nothing to change.
* **Realtime timeout** (default 2500 ms) is how long WLED holds the last
  received frame before falling back to its own effects. ledwall pushes a
  black frame on shutdown, so the wall goes dark cleanly rather than freezing
  on the last image.
* Turn **off** "Receive UDP notifications" between the two controllers if you
  have them syncing to each other: ledwall drives each one independently and
  cross-sync will fight it.

## 5. Verifying

```bash
ledwall doctor                 # reachability + LED counts vs your config
ledwall test corner            # lights only the true top-left 5x5
ledwall test border            # red top, blue bottom, green left, yellow right
ledwall map --by output        # one output at a time, in order
ledwall map --by line --hold 1 # one strip run at a time
```

Common results and what they mean:

| What you see | Cause |
|---|---|
| Colours wrong but positions right | bus Color Order, or RGB/RGBW mismatch |
| Every other row reversed | `wiring.serpentine` or `serpentine_scope` wrong |
| Image mirrored or upside down | `wiring.start_corner` |
| One output dark | wrong GPIO, or Start/Count not matching `ledwall buses` |
| Outputs in the wrong order | reorder the `outputs` / `lines` in your config |
| Bands of stale pixels while moving | dropped DDP packets: get off Wi-Fi |
| Far end of a run dims to red | power injection, not a data problem |
