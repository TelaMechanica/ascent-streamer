# Ascent Streamer - 1.0.5-rc2-public1

**Release candidate:** 1.0.5-rc2 adds a source-selected renderer for the observed older 1080p stream and measured playback diagnostics. Moving-reference tests reach about 30 display fps with bounded frame age. **Actual older-VRX motion playback remains unqualified:** the previous live motion test failed. The experimental buffered receiver stays disabled. The approved MAX preset remains unchanged.

**Boot release notice:** 1.0.2 and 1.0.3 are withdrawn after a boot regression. Release 1.0.4 preserves valid console devices and uses framebuffer console mapping, verified by reboot on this Pi.

A local, unattended viewer for Raspberry Pi Zero 2 W and the **pre-April-2025 iUniker MZP351HV00BR GPIO screen**, tested on 32-bit Raspberry Pi OS Bookworm. No desktop or keyboard interaction is required.

## Everyday use

Automatic mode finds SSIDs containing `ASCENT` anywhere in the name. A visible preferred network takes priority. It joins using the saved password and opens `rtsp://192.168.0.1:554/live.ch01`. Internet access is never required.

After 60 seconds without a successful join, the controller starts its hotspot; activation takes a few additional seconds.

| Item | Value |
|---|---|
| Hotspot SSID / password | `streamer` / `streamer` |
| Hotspot page | `http://10.42.0.1:8080` |
| Page login | `streamer` / `streamer` |
| SSH | `ssh streamer@10.42.0.1`, using your Pi SSH password |

In client mode, use your Pi's assigned address for the page and SSH.

The page configures the preferred network, credentials, video URL and playback. **Setup on saved Wi-Fi** returns to the saved home profile when available. **Automatic viewer** enables discovery and fallback.

Metadata comes from the active playback pipeline, so the configuration page never opens another stream. Some VRX versions stream their own UI when unlinked; older units require an active VTX before RTSP starts.

## Display

- **WAITING FOR VRX** while searching or reconnecting.
- **WAITING FOR VTX** after joining the VRX Wi-Fi, until video arrives. Older VRX units may require an active drone before offering RTSP.
- **SETUP MODE** in red when Setup on saved Wi-Fi is selected.
- **WIFI AP ACTIVE** once the hotspot is ready.
- **Live video** supersedes the automatic-mode splashes when decoded frames arrive.

Every splash displays the installed viewer version from `VERSION` in the lower-right corner. Release filenames and the configuration API use the same version. A source fingerprint accompanies the version so experimental code changes are distinguishable.

The supplied images are in `assets/`. Display ownership is coordinated so the waiting image stays visible while the player opens the stream.

Release 1.0.4 retains valid kernel consoles and maps the text console away from the sole framebuffer with `fbcon=map:1`. Filesystem checks still run. The viewer starts directly as a system service; local autologin is unnecessary. SSH remains enabled. Boot settings are backed up by `sudo /usr/local/sbin/ascent-configure-headless` before changes. A reboot and both splash transitions were verified with an empty backing framebuffer. Brief blank graphics handoffs can still occur.

The older panel uses native 320×480 KMS timings, padded RGB565 GPIO routing, GPIO18 backlight and a 10,670,400 Hz requested clock. KMS reports 60 Hz. Video is rotated into landscape 480×320; console rotation is configured separately. The vendor installer was never executed. Touchscreen and battery sensing are not configured.

## Frozen MAX playback preset

| Setting | Approved value |
|---|---|
| Transport / jitter buffer | UDP / 200 ms |
| Decoder | Hardware V4L2 H.264 |
| Scaling | Software, aspect preserved |
| Framebuffer | RGB565 (`RGB16`) |
| Output FPS limit | 30, based on wall-clock time |
| Rotation | 90° clockwise |
| No-frame watchdog | 12 seconds |

The timestamp-based `videorate` stage was removed. The user explicitly approved this MAX pipeline; these settings are frozen in `playback-baseline.json` and available through **Use proven preset**.

For the observed `1920x1080` stream with colorimetry `1:4:16:4`, the viewer selects DMA-backed hardware resizing, a small cached conversion, and bounded raw-frame queues. FPS limiting drops decoded frames; it never discards compressed reference pictures. RTSP jitter-buffer losses are reported separately. Selection uses stream metadata, not the AP name. A failed hardware renderer falls back once to the standard path; reconnecting retries selection. Other dimensions or metadata retain the standard path. The page reports measured display FPS, the active renderer, and fallback status. Internal PTS backlog is diagnostic information, not camera-to-screen latency.

The observed older VRX spectator60 stream advertises colorimetry `1:4:16:4`, which the Pi hardware decoder rejects. The viewer conditionally interprets that exact tuple as BT.709; other stream metadata is unchanged. This fixes negotiation, not overall playback quality. One TCP trial looked good, but repeated trials and a moving-camera test failed. No transport is currently qualified for this older VRX. An experimental buffered UDP receiver is retained for diagnostics but disabled for automatic selection. The MAX baseline and VRX encoder settings are unchanged.

The VRX encoder is unchanged. The inspected stream reports H.264 High, 1920×1080, 60 fps. Display FPS does not reconfigure the source. The UI permits 0–1000 ms buffering; that setting is not a measurement or guarantee of total camera-to-display latency.

## Installation

For the single-file installer and SD-card preparation steps, see [INSTALL.md](INSTALL.md).

Copy this release to the Pi, join setup/home Wi-Fi, then run:

```sh
sudo bash scripts/install.sh --display-pre2025
sudo reboot
```

Use the display flag only for the verified older panel. Existing unrelated DPI settings require manual review. The installer backs up boot and NetworkManager settings, saves the active home Wi-Fi UUID, installs dependencies, enables SSH, disables the local LCD console, and starts in setup mode.

Automatic discovery is enabled by default and starts on the next boot. Reboot after installing; no configuration-page action is required. Pass `--setup` only when you deliberately want to hold home Wi-Fi and pause automatic joining. Other saved network and playback settings are preserved.

## Recovery and maintenance

The controller owns `ascent-client` and `ascent-hotspot`, preserving the home profile. Failed activations are cancelled before retrying, and failure details are retained. Candidates rotate across failed or hung attempts so one network cannot monopolize retries. Client power saving is disabled. The hotspot stays up for 120 seconds before retrying, and remains available while management clients are associated. Because there is one radio, configuration clients may disconnect during a join attempt. A driver that cannot scan in AP mode is handled by temporarily leaving and restoring that mode.

| Item | Location |
|---|---|
| Application / page | `/opt/ascent-viewer/viewer.py`, `web/index.html` |
| Root-only settings | `/etc/ascent-viewer/config.json` |
| Previous settings | `/etc/ascent-viewer/config.previous.json` |
| Saved home profile | `/etc/ascent-viewer/setup-network.uuid` |
| Logs | `journalctl -u ascent-viewer -f` |

Settings use atomic replacement and `fsync`; a damaged primary JSON falls back to the previous copy. Unexpected worker failure restarts the entire process group through systemd. The service has a read-only system filesystem, protected home directories and private temporary storage.

Restore home Wi-Fi and disable takeover temporarily:

```sh
sudo /usr/local/sbin/ascent-recover-setup
```

Before maintenance that changes Wi-Fi, arm an independent rollback:

```sh
sudo systemd-run --unit=ascent-maintenance-rollback --on-active=180s \
  /usr/local/sbin/ascent-recover-setup
sudo systemctl is-active ascent-maintenance-rollback.timer
```

Cancel it after confirming access is restored: `sudo systemctl stop ascent-maintenance-rollback.timer`.

For code updates from Windows:

```powershell
.\deploy.ps1 -PiAddress PI_ADDRESS -PiHostKey VERIFIED_HOST_KEY -PiPassword YOUR_PI_PASSWORD
```

Deployment backs up replaced files and reloads systemd, preserving settings. Restart explicitly with `sudo systemctl restart ascent-viewer`. Use the installer for first installation.

Other commands:

```sh
sudo systemctl restart ascent-viewer
sudo systemctl disable --now ascent-viewer
sudo poweroff
```

Backups under `/var/backups/ascent-*` contain credentials and must remain private. Initial settings are in `/var/backups/ascent-initial`; playback snapshots must also remain private.

## Validation and release

```sh
python -m unittest -v test_viewer.py test_capture_rtsp.py test_headless.py test_inspect_h264.py
```

See [VALIDATION.md](VALIDATION.md) for hardware results and [INSPECTION.md](INSPECTION.md) for initial findings. The build helper `scripts/build-release.ps1` creates a ZIP and SHA-256 checksum in `dist/`. Releases exclude private captures, device configuration backups and the research handoff.

Network transitions leave hotspot mode before scanning, reset the radio to station mode, and wait for NetworkManager readiness. Scans wait for NetworkManager's completed-scan timestamp before reading its AP cache. Association and DHCP were observed on the original bench setup. The recovery helper restores saved Wi-Fi and device autoconnect.

## Public source

Published as Ascent Streamer. Internal service and installation paths retain `ascent-viewer` for compatibility. Package archives use `ascent-streamer`. Device-specific observations have been anonymized; no internal Git history, captures or saved configuration are included.

The hotspot and web-login defaults documented above are product defaults, not personal credentials. Set up the Pi with your own SSH password.
