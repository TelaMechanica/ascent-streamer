#!/usr/bin/python3
"""Independent rollback: disable takeover, restore saved setup Wi-Fi, restart UI."""
import json
import os
from pathlib import Path
import subprocess
import sys

if '--worker' not in sys.argv:
    state = subprocess.run(['systemctl', 'show', '-p', 'ActiveState', '--value', 'ascent-setup-recovery.service'],
                           capture_output=True, text=True).stdout.strip()
    if state in ('active', 'activating', 'deactivating'):
        print('Setup-network recovery is already running.')
        raise SystemExit(0)
    subprocess.run(['systemd-run', '--collect', '--unit=ascent-setup-recovery',
                    '/usr/bin/python3', str(Path(__file__).resolve()), '--worker'], check=True)
    print('Setup-network recovery scheduled independently of this SSH connection.')
    raise SystemExit(0)

root = Path('/etc/ascent-viewer')
subprocess.run(['systemctl', 'stop', 'ascent-viewer'], timeout=20)
config = root / 'config.json'
data = json.loads(config.read_text())
data['setup_mode'] = True
temp = root / 'config.recovery.tmp'
with temp.open('w') as handle:
    json.dump(data, handle, indent=2)
    handle.flush()
    os.fsync(handle.fileno())
temp.chmod(0o600)
temp.replace(config)
uuid = (root / 'setup-network.uuid').read_text().strip()
subprocess.run(['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes'], timeout=10)
subprocess.run(['nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'yes'], timeout=10)
subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], timeout=5)
sys.path.insert(0, '/opt/ascent-viewer')
import viewer
if viewer.active() == 'ascent-hotspot':
    viewer.prepare_station()
if uuid:
    subprocess.run(['nmcli', '--wait', '25', 'connection', 'up', 'uuid', uuid], timeout=30)
subprocess.run(['systemctl', 'start', 'ascent-viewer'], timeout=20)
