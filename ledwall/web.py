"""Headless status page and MJPEG preview.

The Pi has no screen, so this is how you see what the wall is doing: point a
browser on your laptop at http://<pi>:8080. Stdlib only - no Flask - to keep
the install on a Pi 3B+ light.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from .palettes import names as palette_names

PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ledwall</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0b0d10;color:#e6e9ef;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
 header{padding:14px 20px;border-bottom:1px solid #1e242c;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
 main{padding:20px;display:grid;gap:20px;max-width:1100px}
 .panel{background:#12161b;border:1px solid #1e242c;border-radius:10px;padding:14px}
 .panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8b96a5;margin:0 0 10px}
 img{width:100%;image-rendering:pixelated;border-radius:6px;background:#000;display:block}
 .grid{display:grid;gap:20px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
 table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
 td{padding:3px 0;border-bottom:1px solid #171c22}
 td:last-child{text-align:right;color:#a9b4c2}
 label{display:flex;align-items:center;gap:10px;margin:8px 0}
 label span{width:90px;color:#8b96a5}
 input[type=range]{flex:1}
 select{background:#1a1f26;color:#e6e9ef;border:1px solid #2a323c;border-radius:6px;padding:5px 8px;flex:1}
 .err{color:#ff8a7a}
 .ok{color:#6ee7a8}
</style>
<header><h1>ledwall</h1><span id="hdr" class="ok">connecting…</span></header>
<main>
 <div class="grid">
  <div class="panel"><h2>Wall output</h2><img id="wall" src="/stream/wall.mjpg"></div>
  <div class="panel"><h2>Camera</h2><img id="cam" src="/stream/camera.mjpg"></div>
 </div>
 <div class="grid">
  <div class="panel"><h2>Status</h2><table id="stats"></table></div>
  <div class="panel"><h2>Controls</h2>
    <label><span>Brightness</span><input type="range" id="bri" min="0" max="100" value="35">
      <b id="brival" style="width:3em;text-align:right">35%</b></label>
    <label><span>Palette</span><select id="pal"></select></label>
    <label><span>Mode</span><select id="mode">
      <option>fill</option><option>outline</option><option>glow</option></select></label>
    <div class="panel" style="margin-top:12px"><h2>Controllers</h2><table id="ctrl"></table></div>
  </div>
 </div>
</main>
<script>
const $=s=>document.querySelector(s);
for(const p of PALETTES){const o=document.createElement('option');o.textContent=p;$('#pal').appendChild(o);}
function post(body){fetch('/api/control',{method:'POST',body:JSON.stringify(body)});}
$('#bri').oninput=e=>{$('#brival').textContent=e.target.value+'%';post({brightness:e.target.value/100});};
$('#pal').onchange=e=>post({palette:e.target.value});
$('#mode').onchange=e=>post({mode:e.target.value});
let synced=false;
async function poll(){
 try{
  const s=await (await fetch('/api/state')).json();
  $('#hdr').textContent=`${s.grid.width}x${s.grid.height} · ${s.leds} LEDs · ${s.output_fps} fps out · ${s.infer_fps} fps infer`;
  $('#hdr').className='ok';
  const rows=[['Source',s.source],['Output fps',s.output_fps],['Inference fps',s.infer_fps],
   ['Inference time',s.infer_ms+' ms'],['Frames sent',s.frames_out],['Lit fraction',(s.coverage*100).toFixed(1)+'%'],
   ['Camera',s.camera.backend+' ('+s.camera.frames+' frames)'],['Uptime',Math.round(s.uptime)+'s']];
  if(s.camera.last_error)rows.push(['Camera error',s.camera.last_error]);
  if(s.infer_error)rows.push(['Inference error',s.infer_error]);
  $('#stats').innerHTML=rows.map(r=>`<tr><td>${r[0]}</td><td${String(r[0]).includes('error')?' class="err"':''}>${r[1]}</td></tr>`).join('');
  $('#ctrl').innerHTML=Object.entries(s.controllers).map(([n,c])=>
   `<tr><td>${n}</td><td${c.errors?' class="err"':''}>${c.frames} frames, ${c.packets} pkts${c.errors?', '+c.errors+' errors':''}</td></tr>`).join('');
  if(!synced){synced=true;$('#bri').value=Math.round(s.brightness*100);$('#brival').textContent=Math.round(s.brightness*100)+'%';
   $('#pal').value=s.palette;$('#mode').value=s.mode;}
 }catch(e){$('#hdr').textContent='disconnected';$('#hdr').className='err';}
}
poll();setInterval(poll,1000);
</script>
"""


def _encode(img: np.ndarray, quality: int) -> bytes | None:
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pipeline = None
    webcfg = None

    def log_message(self, *args):  # keep the journal readable
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            page = PAGE.replace("PALETTES", json.dumps(palette_names()))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(self.pipeline.state()).encode(), "application/json")
        elif path in ("/stream/wall.mjpg", "/stream/camera.mjpg"):
            self._stream(wall=path.endswith("wall.mjpg"))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/control":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        p = self.pipeline
        try:
            if "brightness" in body:
                p.set_brightness(float(body["brightness"]))
            if "palette" in body:
                p.set_palette(str(body["palette"]))
            if "mode" in body:
                p.set_mode(str(body["mode"]))
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return
        self._send(200, json.dumps({"ok": True}).encode(), "application/json")

    def _stream(self, *, wall: bool) -> None:
        import cv2

        cfg = self.webcfg
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        period = 1.0 / 15.0  # cap preview rate; the Pi has better things to do
        try:
            while True:
                start = time.monotonic()
                wall_img, cam_img = self.pipeline.previews()
                img = wall_img if wall else cam_img
                if img is None:
                    time.sleep(0.2)
                    continue
                if wall:
                    s = max(cfg.preview_scale, 1)
                    img = cv2.resize(
                        img, (img.shape[1] * s, img.shape[0] * s), interpolation=cv2.INTER_NEAREST
                    )
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                else:
                    h, w = img.shape[:2]
                    if w > 480:
                        img = cv2.resize(img, (480, int(h * 480 / w)), interpolation=cv2.INTER_AREA)
                jpg = _encode(img, cfg.jpeg_quality)
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")
                self.wfile.write(str(len(jpg)).encode())
                self.wfile.write(b"\r\n\r\n")
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                elapsed = time.monotonic() - start
                if elapsed < period:
                    time.sleep(period - elapsed)
        except (BrokenPipeError, ConnectionResetError):
            pass


class WebServer:
    def __init__(self, pipeline, cfg) -> None:
        handler = type("Handler", (_Handler,), {"pipeline": pipeline, "webcfg": cfg})
        self.server = ThreadingHTTPServer((cfg.host, cfg.port), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="web", daemon=True)

    def start(self) -> "WebServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
