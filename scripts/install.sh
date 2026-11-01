#!/bin/bash
# Install from this checkout on Raspberry Pi OS Bookworm. Existing settings survive.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
activate=1
display=0
for arg in "$@"; do
  case "$arg" in
    --activate) activate=1 ;;
    --setup) activate=0 ;;
    --display-pre2025) display=1 ;;
    --help|-h) echo "Usage: sudo bash scripts/install.sh [--setup] [--display-pre2025]"; echo "Automatic discovery starts on the next boot by default; --setup holds home Wi-Fi."; exit 0 ;;
    *) echo "Usage: sudo scripts/install.sh [--setup] [--display-pre2025]" >&2; exit 2 ;;
  esac
done
if [[ $EUID -ne 0 ]]; then echo "Run this installer with sudo." >&2; exit 1; fi
if ! id streamer >/dev/null 2>&1; then
  echo "Create the user streamer in Raspberry Pi Imager before installing." >&2; exit 1
fi
if [[ ! -r /etc/os-release || ! -r /proc/device-tree/model ]]; then
  echo "Run this script on the Raspberry Pi, after booting Raspberry Pi OS." >&2; exit 1
fi
source /etc/os-release
if [[ ${VERSION_CODENAME:-} != bookworm || $(getconf LONG_BIT) != 32 ]]; then
  echo "This release is qualified for 32-bit Raspberry Pi OS Bookworm. No changes were made." >&2; exit 1
fi
if ! grep -aq 'Raspberry Pi Zero 2 W' /proc/device-tree/model; then
  echo "This installer targets Raspberry Pi Zero 2 W. No changes were made." >&2; exit 1
fi
if ! command -v nmcli >/dev/null || ! systemctl is-active --quiet NetworkManager; then
  echo "Connect the Pi to setup/home Wi-Fi using NetworkManager before installing." >&2; exit 1
fi
setup_uuid=''
if [[ ! -f /etc/ascent-viewer/setup-network.uuid ]]; then
  setup_uuid=$(nmcli -g GENERAL.CON-UUID device show wlan0)
  if ! python3 -c 'import sys,uuid; uuid.UUID(sys.argv[1])' "$setup_uuid" 2>/dev/null; then
    echo "Join setup/home Wi-Fi on wlan0 before installing. No changes were made." >&2; exit 1
  fi
fi
backup_dir="/var/backups/ascent-install-$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "$backup_dir" /etc/ascent-viewer
cp -a /etc/NetworkManager "$backup_dir/"
cp -a /boot/firmware/config.txt /boot/firmware/cmdline.txt "$backup_dir/"
systemctl get-default > "$backup_dir/default-target.txt"
if [[ -d /opt/ascent-viewer ]]; then cp -a /opt/ascent-viewer "$backup_dir/application"; fi
if [[ -f /etc/ascent-viewer/config.json ]]; then cp -a /etc/ascent-viewer/config.json "$backup_dir/"; fi
if systemctl is-active --quiet ascent-viewer; then systemctl stop ascent-viewer; fi
if [[ ! -f /etc/ascent-viewer/setup-network.uuid ]]; then
  connection_name=$(nmcli -g GENERAL.CONNECTION device show wlan0)
  if [[ $connection_name == ascent-* ]]; then
    echo "Install while joined to setup/home Wi-Fi so its recovery profile can be saved." >&2
    exit 1
  fi
  printf '%s\n' "$setup_uuid" > /etc/ascent-viewer/setup-network.uuid
  chmod 600 /etc/ascent-viewer/setup-network.uuid
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  network-manager dnsmasq-base iw python3-gi gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-x ffmpeg
install -d -m 755 /opt/ascent-viewer/web
install -m 644 "$source_dir/viewer.py" "$source_dir/legacy_receiver.py" "$source_dir/capture_rtsp.py" "$source_dir/inspect_h264.py" "$source_dir/VERSION" "$source_dir/README.md" "$source_dir/VALIDATION.md" "$source_dir/INSPECTION.md" /opt/ascent-viewer/
install -m 644 "$source_dir/web/index.html" /opt/ascent-viewer/web/
install -m 644 "$source_dir/assets/waiting.png" "$source_dir/assets/waiting-vtx.png" "$source_dir/assets/setup.png" "$source_dir/assets/hotspot.png" /opt/ascent-viewer/
install -m 644 "$source_dir/ascent-viewer.service" /etc/systemd/system/
install -m 755 "$source_dir/scripts/recover-setup.py" /usr/local/sbin/ascent-recover-setup
install -m 755 "$source_dir/scripts/configure_headless.py" /usr/local/sbin/ascent-configure-headless
install -m 600 "$source_dir/playback-baseline.json" /etc/ascent-viewer/playback-baseline.json
python3 - "$activate" "$display" <<'PY'
import pathlib, sys, uuid
sys.path.insert(0, '/opt/ascent-viewer')
import viewer
uuid.UUID((viewer.ROOT/'setup-network.uuid').read_text().strip())
if not viewer.CONFIG.exists():
    viewer.write_config(viewer.DEFAULT)
config = viewer.read_config()
config['setup_mode'] = sys.argv[1] != '1'
viewer.write_config(config)
if sys.argv[2] == '1':
    config = pathlib.Path('/boot/firmware/config.txt')
    text = config.read_text()
    if 'vc4-kms-dpi-generic' in text and '# BEGIN ASCENT DPI' not in text:
        raise SystemExit('Existing DPI configuration needs manual review; it was left unchanged.')
    if '# BEGIN ASCENT DPI' not in text:
        if 'dtoverlay=vc4-kms-v3d' not in text:
            text += '\n[all]\ndtoverlay=vc4-kms-v3d\n'
        text += '''
# BEGIN ASCENT DPI
[all]
dtoverlay=vc4-kms-dpi-generic
dtparam=hactive=320,hfp=20,hsync=10,hbp=10
dtparam=vactive=480,vfp=10,vsync=2,vbp=2
dtparam=clock-frequency=10670400
dtparam=hsync-invert,vsync-invert,pixclk-invert
dtparam=rgb565-padhi
dtparam=backlight-gpio=18
gpio=18=op,dh,pd
# END ASCENT DPI
'''
        config.write_text(text)
    cmdline = pathlib.Path('/boot/firmware/cmdline.txt')
    words = [w for w in cmdline.read_text().split() if not w.startswith(('fbcon=rotate:', 'consoleblank=', 'vt.global_cursor_default=', 'loglevel=')) and w not in ('quiet', 'logo.nologo')]
    words += ['fbcon=rotate:1', 'consoleblank=0', 'vt.global_cursor_default=0', 'quiet', 'loglevel=3', 'logo.nologo']
    cmdline.write_text(' '.join(words)+'\n')
PY
/usr/local/sbin/ascent-configure-headless
for element in v4l2h264dec kmssink pngdec imagefreeze videoflip; do
  gst-inspect-1.0 "$element" >/dev/null
done
python3 -m py_compile /opt/ascent-viewer/viewer.py
systemd-analyze verify /etc/systemd/system/ascent-viewer.service
systemctl daemon-reload
systemctl set-default multi-user.target
systemctl enable --now ssh
systemctl enable ascent-viewer
if [[ $activate == 0 ]]; then systemctl restart ascent-viewer; fi
echo "Installed. Backup: $backup_dir"
echo "Configuration page: http://<Pi-address>:8080 (streamer / streamer)"
if [[ $activate == 0 ]]; then echo "Explicit setup mode: home Wi-Fi is preserved; automatic joining is paused."; fi
echo "Reboot to start automatic discovery and apply the headless display settings."
