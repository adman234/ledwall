#!/usr/bin/env bash
# ledwall installer for Raspberry Pi OS (and most Debian/Ubuntu machines).
#
#   curl -fsSL .../install.sh | sudo bash
# or, from a clone:
#   sudo ./scripts/install.sh
#
# Idempotent: safe to re-run to upgrade an existing install.
set -euo pipefail

PREFIX="${PREFIX:-/opt/ledwall}"
CONFIG_DIR="${CONFIG_DIR:-/etc/ledwall}"
CONFIG_FILE="$CONFIG_DIR/ledwall.yaml"
SERVICE_NAME="ledwall"
WITH_MEDIAPIPE=0
INSTALL_SERVICE=1
RUN_USER="${SUDO_USER:-${USER:-root}}"

usage() {
  cat <<EOF
Usage: sudo $0 [options]

  --with-mediapipe   also install mediapipe (best masks; 64-bit OS only)
  --no-service       don't install/enable the systemd service
  --user USER        user to run the service as (default: $RUN_USER)
  --prefix DIR       install location (default: $PREFIX)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-mediapipe) WITH_MEDIAPIPE=1; shift ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="$(uname -m)"
GLIBC="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo '?')"

echo "==> ledwall install"
echo "    source:   $SRC_DIR"
echo "    prefix:   $PREFIX"
echo "    arch:     $ARCH   glibc: $GLIBC"
echo "    run as:   $RUN_USER"

if [[ "$ARCH" != "aarch64" && "$ARCH" != "x86_64" ]]; then
  echo "    NOTE: 32-bit userland detected. mediapipe has no wheel for it;"
  echo "          tflite-runtime does (Bookworm only). mog2 always works."
  WITH_MEDIAPIPE=0
fi

echo "==> apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# libglib2.0-0 + libgomp1 are what the opencv/numpy wheels actually need at
# runtime; v4l-utils is for debugging the webcam.
apt-get install -y -qq python3-venv python3-dev libglib2.0-0 libgomp1 v4l-utils curl >/dev/null

echo "==> python venv at $PREFIX/venv"
mkdir -p "$PREFIX"
python3 -m venv "$PREFIX/venv"
PIP="$PREFIX/venv/bin/pip"
"$PIP" install -q --upgrade pip wheel

echo "==> ledwall + core dependencies"
"$PIP" install -q "$SRC_DIR"

# --- segmentation backend -------------------------------------------------
# Try the light LiteRT runtimes first. ai-edge-litert is aarch64-only;
# tflite-runtime also ships armv7l wheels but needs glibc >= 2.34 (Bookworm).
BACKEND="mog2"
echo "==> segmentation runtime"
if "$PIP" install -q ai-edge-litert 2>/dev/null; then
  BACKEND="tflite (ai-edge-litert)"
elif "$PIP" install -q tflite-runtime 2>/dev/null; then
  BACKEND="tflite (tflite-runtime)"
else
  echo "    no LiteRT wheel for this platform; falling back to OpenCV mog2"
fi

if [[ $WITH_MEDIAPIPE -eq 1 ]]; then
  echo "==> mediapipe"
  if "$PIP" install -q mediapipe; then
    # mediapipe depends on opencv-contrib-python, the GUI build, which
    # replaces our headless cv2 and then fails to import without libGL.
    # Swap it for the headless variant so nothing needs an X stack.
    "$PIP" install -q --force-reinstall opencv-contrib-python-headless
    BACKEND="mediapipe"
  else
    echo "    mediapipe install failed; keeping $BACKEND"
  fi
fi
echo "    backend: $BACKEND"

echo "==> segmentation model"
MODEL_DIR="$PREFIX/models"
mkdir -p "$MODEL_DIR"
MODEL="$MODEL_DIR/selfie_segmenter.tflite"
if [[ ! -f "$MODEL" ]]; then
  curl -fsSL -o "$MODEL.part" \
    "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/1/selfie_segmenter.tflite" \
    && mv "$MODEL.part" "$MODEL" && echo "    downloaded $(du -h "$MODEL" | cut -f1)"
else
  echo "    already present"
fi

echo "==> config"
mkdir -p "$CONFIG_DIR"
if [[ -f "$CONFIG_FILE" ]]; then
  echo "    keeping existing $CONFIG_FILE"
else
  cp "$SRC_DIR/config/wall-50x100.yaml" "$CONFIG_FILE"
  # Point the config at the model we just fetched.
  sed -i "s|^  model_path:.*|  model_path: $MODEL|" "$CONFIG_FILE" 2>/dev/null || true
  grep -q "model_path" "$CONFIG_FILE" || sed -i "/^source:/a\\  model_path: $MODEL" "$CONFIG_FILE"
  echo "    wrote $CONFIG_FILE  <-- EDIT the controller IPs before running"
fi
chown -R "$RUN_USER" "$CONFIG_DIR" || true

# The service user needs the camera and a writable model cache.
usermod -aG video "$RUN_USER" 2>/dev/null || true

echo "==> UDP send buffer"
# 5,000 RGBW pixels at 30fps is ~600 kB/s per controller in bursts of 7
# packets; the default 208 kB wmem is enough, but headroom avoids drops.
cat > /etc/sysctl.d/60-ledwall.conf <<EOF
net.core.wmem_max = 2097152
net.core.wmem_default = 1048576
EOF
sysctl -q --system >/dev/null 2>&1 || true

if [[ $INSTALL_SERVICE -eq 1 ]]; then
  echo "==> systemd service"
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONFIG@|$CONFIG_FILE|g" -e "s|@USER@|$RUN_USER|g" \
    "$SRC_DIR/scripts/ledwall.service" > "/etc/systemd/system/$SERVICE_NAME.service"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  echo "    enabled (not started - edit the config first)"
fi

ln -sf "$PREFIX/venv/bin/ledwall" /usr/local/bin/ledwall

cat <<EOF

==> done

  1. Edit the controller IPs:      sudo nano $CONFIG_FILE
  2. Check everything:             ledwall doctor
  3. Numbers to type into WLED:    ledwall buses
  4. Verify wiring on the wall:    ledwall map --by output
  5. Start it:                     sudo systemctl start $SERVICE_NAME
     Logs:                         journalctl -u $SERVICE_NAME -f
     Web preview:                  http://\$(hostname -I | awk '{print \$1}'):8080

EOF
