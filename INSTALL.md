# Install on a new Raspberry Pi Zero 2 W

Use **Raspberry Pi OS Lite Bookworm, 32-bit**, the same OS family tested on the working viewer. The installer checks the board and OS before making changes.

## Prepare the SD card

In Raspberry Pi Imager, configure:

- Username: `streamer`
- Password: choose your own SSH password
- SSH enabled
- Your home Wi-Fi and correct wireless country

Boot the Pi and find its home-network IP address. Keep the Pi on home Wi-Fi during installation so it can download packages and retain a recovery connection.

## Copy and install

In the folder containing the downloaded installer, substitute the new Pi's address for `NEW_PI_IP`:

```powershell
scp install-ascent-streamer-1.0.5-rc2-public1.run streamer@NEW_PI_IP:/home/streamer/
ssh streamer@NEW_PI_IP
```

On the Pi:

```sh
sudo bash install-ascent-streamer-1.0.5-rc2-public1.run --display-pre2025
sudo reboot
```

`--display-pre2025` is only for the older iUniker panel we tested. Do not use that flag for a newer or different display without verifying its timings. The script never runs the old vendor installer.

The single-file installer includes the application and both splash images. It verifies the embedded release checksum before extracting into a temporary directory, runs the installer, and cleans up afterward. No separate unzip utility is needed.

The ZIP is also available for source inspection or manual installation: extract it with `python3 -m zipfile -e ascent-streamer-1.0.5-rc2-public1.zip ascent-streamer`, then run `sudo bash ascent-streamer/scripts/install.sh --display-pre2025`.

## Automatic startup

After reboot, the viewer automatically scans for and joins an ASCENT network. No login or configuration-page step is required. Configuration is available at `http://<Pi-client-address>:8080`, with login `streamer` / `streamer`, or at the fallback hotspot address below.

The Pi will leave home Wi-Fi and join an SSID containing `ASCENT`, using password `12345678`. Its RTSP URL, UDP transport, 200 ms buffer, display settings and custom splash screens are installed from the frozen working release. It will create `streamer` / `streamer` if joining fails for 60 seconds; then use `http://10.42.0.1:8080`.

Installation preserves home Wi-Fi until you reboot. Automatic discovery is the default for that next boot. Use `--setup` only for deliberate maintenance: it pauses automatic joining and starts the configuration page on saved home Wi-Fi.

## What the installer includes

Dependency installation, supported KMS display configuration, desktop-free boot with no local CLI, SSH, network discovery/fallback, the configuration page, both splash images, approved playback settings, and the recovery command. Existing configurations are backed up under `/var/backups/ascent-install-*` before editing.

Recovery over SSH:

```sh
sudo /usr/local/sbin/ascent-recover-setup
```

This schedules an independent recovery worker to return to saved setup Wi-Fi and stop automatic takeover; it continues if SSH disconnects. The fresh-card installation path has been syntax checked; the application's playback and recovery behavior were validated on the original Pi. A second clean SD card has not yet been used for a complete reinstall test.
