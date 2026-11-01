#!/usr/bin/python3
"""Local FPV appliance: NetworkManager, supervised GStreamer, authenticated UI."""
import base64
import copy
import hmac
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path('/etc/ascent-viewer')
VERSION_FILE = Path(__file__).with_name('VERSION')
VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else 'development'
BUILD = hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes()
                               for name in ('viewer.py', 'capture_rtsp.py', 'legacy_receiver.py', 'VERSION')
                               if Path(__file__).with_name(name).exists())).hexdigest()[:8]
CONFIG = ROOT / 'config.json'
RUN = Path('/run/ascent-viewer')
LOCK = threading.RLock()
DEFAULT = dict(setup_mode=False, preferred_ssid='', network_password='12345678',
               static_address='',
               rtsp_url='rtsp://192.168.0.1:554/live.ch01', transport='udp',
               latency_ms=200, rotation='90r', max_fps=30, watchdog_seconds=12,
               scaler='software')
STATUS = dict(network='starting', playback='starting')
TOKEN = secrets.token_urlsafe(24)
CHANGED = threading.Event()
RECONNECT = threading.Event()
HOTSPOT_RETRY_SECONDS = 120  # Keep fallback access usable between station scans.
EXPERIMENTAL_LEGACY_RECEIVER = False  # Withdrawn after the moving-camera test failed.
ENABLE_LEGACY_RENDERER = True  # Source-qualified rendering; MAX keeps the standard path.


def command(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, 'LC_ALL': 'C'})


def read_config():
    with LOCK:
        try:
            data = json.loads(CONFIG.read_text())
            if not isinstance(data, dict):
                raise ValueError('Configuration must be an object')
        except (OSError, ValueError):
            data = json.loads(CONFIG.with_suffix('.previous.json').read_text())
            update(config_notice='Recovered the previous saved configuration')
        return {**DEFAULT, **data}


def write_config(data):
    with LOCK:
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        if CONFIG.exists():
            try:
                json.loads(CONFIG.read_text())
                previous = CONFIG.with_suffix('.previous.json')
                previous.write_bytes(CONFIG.read_bytes())
                previous.chmod(0o600)
            except ValueError:
                pass
        temp = CONFIG.with_suffix('.tmp')
        with temp.open('w') as output:
            json.dump(data, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temp.chmod(0o600)
        temp.replace(CONFIG)
        if os.name == 'posix':
            descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def update(**data):
    with LOCK:
        for key in ('network', 'playback'):
            if key in data and data[key] != STATUS.get(key):
                print(f'{key}: {data[key]}', flush=True)
        STATUS.update(data)


def runtime_metrics():
    """Return diagnostic samples with their age, including after playback stops."""
    try:
        path = RUN / 'metrics.json'
        if path.stat().st_size > 65536:
            return None
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return None
        at = float(data['at'])
        if not math.isfinite(at):
            return None
        return {**data, 'sample_age_seconds': max(0, time.time()-at)}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def observe_rtp(metrics, state, header):
    """Sequence gaps after jitter buffering are not automatically network loss."""
    metrics['rtp_packets'] += 1
    if len(header) < 12 or header[0] >> 6 != 2:
        metrics['rtp_invalid_headers'] = metrics.get('rtp_invalid_headers', 0)+1
        return
    current = int.from_bytes(header[2:4], 'big')
    ssrc = int.from_bytes(header[8:12], 'big')
    if state.get('ssrc') != ssrc:
        if 'ssrc' in state:
            metrics['rtp_ssrc_changes'] = metrics.get('rtp_ssrc_changes', 0)+1
        state.update(ssrc=ssrc, sequence=current)
        return
    delta = (current-state['sequence']) & 65535
    if delta == 0:
        metrics['rtp_duplicates'] = metrics.get('rtp_duplicates', 0)+1
    elif delta >= 32768:
        metrics['rtp_reordered'] = metrics.get('rtp_reordered', 0)+1
    else:
        if delta > 1:
            metrics['rtp_gap_events'] += 1
            metrics['rtp_missing_packets'] += delta-1
        state['sequence'] = current


def playback_backlog(metrics):
    """PTS distances inside the Pi pipeline, not camera-to-screen latency."""
    result = {}
    for name, newest, oldest in (('decode_backlog_ms', 'compressed_pts', 'decoded_pts'),
                                  ('render_backlog_ms', 'decoded_pts', 'display_pts')):
        values = [metrics.get(newest), metrics.get(oldest)]
        if all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v < 18446744073 for v in values):
            result[name] = max(0, (values[0]-values[1])*1000)
    return result


def scan_timestamp():
    """NetworkManager's completed-scan timestamp, not its cached AP list."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    device = bus.call_sync('org.freedesktop.NetworkManager', '/org/freedesktop/NetworkManager',
                           'org.freedesktop.NetworkManager', 'GetDeviceByIpIface',
                           GLib.Variant('(s)', ('wlan0',)), GLib.VariantType.new('(o)'),
                           Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
    value = bus.call_sync('org.freedesktop.NetworkManager', device,
                          'org.freedesktop.DBus.Properties', 'Get',
                          GLib.Variant('(ss)', ('org.freedesktop.NetworkManager.Device.Wireless', 'LastScan')),
                          GLib.VariantType.new('(v)'), Gio.DBusCallFlags.NONE, 3000, None)
    return value.unpack()[0]


def scan():
    if active() == 'ascent-hotspot':
        if hotspot_has_clients():
            return None  # Preserve access; this is not an empty scan result.
        prepare_station()
    before = scan_timestamp()
    started = time.monotonic()
    r = command(['nmcli', '-g', 'SSID', 'device', 'wifi', 'list',
                 'ifname', 'wlan0', '--rescan', 'yes'], 15)
    if r.returncode:
        raise RuntimeError('Wi-Fi scan failed: ' + r.stderr.strip())
    deadline = started + 20
    while scan_timestamp() <= before:
        if time.monotonic() >= deadline:
            raise RuntimeError('Wi-Fi scan did not complete; cached results rejected')
        time.sleep(.25)
    # Read the cache only after NetworkManager confirms scan completion.
    r = command(['nmcli', '-g', 'SSID', 'device', 'wifi', 'list',
                 'ifname', 'wlan0', '--rescan', 'no'], 5)
    if r.returncode:
        raise RuntimeError('Could not read completed Wi-Fi scan: ' + r.stderr.strip())
    print(f'Wi-Fi scan completed in {time.monotonic() - started:.3f}s', flush=True)
    # JSON-free nmcli escaping is avoided by using a single SSID field.
    available = list(dict.fromkeys(s.replace('\\:', ':').replace('\\\\', '\\')
                                  for s in r.stdout.splitlines() if s))
    if available != STATUS.get('visible_ssids'):
        print('Wi-Fi fresh scan: ' + repr(available), flush=True)
    return available


def hotspot_has_clients():
    """Count radio associations, including clients not using the web page."""
    try:
        result = command(['iw', 'dev', 'wlan0', 'station', 'dump'], 5)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'station query failed')
        return any(line.startswith('Station ') for line in result.stdout.splitlines())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # Never disconnect a possible management client because detection failed.
        update(network_error='Keeping hotspot active: cannot check clients: ' + str(exc))
        return True


def active():
    r = command(['nmcli', '-g', 'GENERAL.CONNECTION', 'device', 'show', 'wlan0'])
    return r.stdout.strip()


def connected():
    r = command(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'wlan0'])
    return r.stdout.startswith('100')


def restore_setup_network():
    command(['nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'yes'], 8)
    saved = ROOT / 'setup-network.uuid'
    if not saved.exists():
        return
    uuid = saved.read_text().strip()
    current = command(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', 'wlan0']).stdout.strip()
    if uuid and current != uuid:
        was_hotspot = active() == 'ascent-hotspot'
        if was_hotspot:
            prepare_station()
        r = command(['nmcli', '--wait', '25', 'connection', 'up', 'uuid', uuid], 30)
        if r.returncode:
            update(network_error='Saved setup Wi-Fi unavailable; preserving current access')
            if was_hotspot:
                hotspot()
        else:
            update(network_error='')


def profiles():
    return command(['nmcli', '-g', 'NAME', 'connection', 'show']).stdout.splitlines()


def prepare_station():
    """Leave Broadcom AP mode before scanning; recreate the supplicant interface."""
    update(network='scanning')
    command(['nmcli', '--wait', '5', 'connection', 'down', 'ascent-hotspot'], 8)
    result = command(['nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'no'], 8)
    if result.returncode:
        raise RuntimeError('Could not suspend competing Wi-Fi autoconnect: ' + result.stderr.strip())
    result = command(['nmcli', 'device', 'set', 'wlan0', 'managed', 'no'], 8)
    if result.returncode:
        raise RuntimeError('Could not release Wi-Fi for station mode: ' + result.stderr.strip())
    try:
        operations = [
            ['ip', 'link', 'set', 'wlan0', 'down'],
            ['iw', 'dev', 'wlan0', 'set', 'type', 'managed'],
            ['ip', 'link', 'set', 'wlan0', 'up'],
        ]
        for operation in operations:
            result = command(operation, 5)
            if result.returncode:
                raise RuntimeError('Could not reset Wi-Fi interface: ' + result.stderr.strip())
    finally:
        result = command(['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes'], 8)
        if result.returncode:
            raise RuntimeError('Could not restore NetworkManager ownership: ' + result.stderr.strip())
    result = command(['nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'no'], 8)
    if result.returncode:
        raise RuntimeError('Could not suspend competing Wi-Fi autoconnect: ' + result.stderr.strip())
    wait_station_ready()


def wait_station_ready(timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = command(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'wlan0'], 3)
        state = result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else ''
        if result.returncode == 0 and state in ('30', '100'):
            return
        time.sleep(.25)
    raise RuntimeError('Wi-Fi station interface did not become ready after leaving hotspot mode')


def hotspot():
    if 'ascent-hotspot' not in profiles():
        r = command(['nmcli', 'connection', 'add', 'type', 'wifi', 'ifname', 'wlan0',
                     'con-name', 'ascent-hotspot', 'ssid', 'streamer',
                     'connection.autoconnect', 'no', '802-11-wireless.mode', 'ap',
                     '802-11-wireless.band', 'bg', '802-11-wireless.channel', '6',
                     'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', 'streamer',
                     'ipv4.method', 'shared', 'ipv4.addresses', '10.42.0.1/24',
                     'ipv6.method', 'disabled'])
        if r.returncode:
            raise RuntimeError(r.stderr)
    r = command(['nmcli', '--wait', '12', 'connection', 'up', 'ascent-hotspot'], 16)
    if r.returncode:
        raise RuntimeError(r.stderr)
    update(network='hotspot', address='10.42.0.1')


def join(ssid, cfg, timeout):
    if 'ascent-client' not in profiles():
        r = command(['nmcli', 'connection', 'add', 'type', 'wifi', 'ifname', 'wlan0',
                     'con-name', 'ascent-client', 'ssid', ssid,
                     'connection.autoconnect', 'no'])
        if r.returncode:
            return False
    r = command(['nmcli', 'connection', 'modify', 'ascent-client',
                 '802-11-wireless.ssid', ssid, 'wifi-sec.key-mgmt', 'wpa-psk',
                 'wifi-sec.psk', cfg['network_password'],
                 '802-11-wireless.powersave', '2',
                 'ipv4.method', 'manual' if cfg['static_address'] else 'auto',
                 'ipv4.addresses', cfg['static_address'], 'ipv4.gateway', '',
                 'ipv4.dad-timeout', '3000',
                 'ipv6.method', 'disabled'])
    if r.returncode:
        update(network_error=r.stderr.strip())
        return False
    update(network='joining ' + ssid)
    r = command(['nmcli', '--wait', str(timeout), 'connection', 'up', 'ascent-client'], timeout + 3)
    if r.returncode == 0:
        update(network='client', connection='ascent-client', ssid=ssid, network_error='',
               address=command(['nmcli', '-g', 'IP4.ADDRESS', 'device', 'show', 'wlan0']).stdout.strip())
        return True
    command(['nmcli', '--wait', '5', 'connection', 'down', 'ascent-client'], 8)
    reason = r.stderr.strip() or r.stdout.strip() or 'NetworkManager activation failed'
    print(f'Wi-Fi join failed for {ssid!r}: {reason}', flush=True)
    update(network_error='Could not join ' + ssid + ': ' + reason)
    return False


def network_loop():
    deadline = time.monotonic() + 60
    ap = False
    previous_mode = None
    connected_settings = None
    attempted = set()
    last_settings = None
    while True:
        try:
            cfg = read_config()
            settings = (cfg['preferred_ssid'], cfg['network_password'], cfg['static_address'])
            if settings != last_settings:
                attempted.clear()
                last_settings = settings
            if cfg['setup_mode']:
                if previous_mode is not True:
                    restore_setup_network()
                previous_mode = True
                setup_connection = active()
                if setup_connection == 'ascent-hotspot' and connected():
                    update(network='hotspot', connection=setup_connection, address='10.42.0.1', ssid='')
                else:
                    update(network='setup: preserving current connection', connection=setup_connection, ssid='')
                deadline = time.monotonic() + 60
                CHANGED.wait(3)
                CHANGED.clear()
                continue
            if previous_mode is True:
                deadline = time.monotonic() + 60
                connected_settings = None
                attempted.clear()
            previous_mode = False
            if active() == 'ascent-client' and connected():
                if connected_settings is None or connected_settings == settings:
                    if STATUS.get('network') != 'client':
                        update(network='client', connection='ascent-client',
                               ssid=command(['nmcli', '-g', '802-11-wireless.ssid', 'connection', 'show', 'ascent-client']).stdout.strip(),
                               address=command(['nmcli', '-g', 'IP4.ADDRESS', 'device', 'show', 'wlan0']).stdout.strip())
                    connected_settings = settings
                    deadline = time.monotonic() + 60
                    ap = False
                    time.sleep(2)
                    continue
                command(['nmcli', '--wait', '5', 'connection', 'down', 'ascent-client'], 8)
                deadline = time.monotonic() + 60
            if not ap and time.monotonic() >= deadline:
                hotspot()
                ap = True
                CHANGED.wait(HOTSPOT_RETRY_SECONDS)
                CHANGED.clear()
                continue
            available = scan()
            if available is None:
                CHANGED.wait(5)
                CHANGED.clear()
                continue
            update(visible_ssids=available)
            candidates = [s for s in available if 'ASCENT' in s]
            preferred = cfg['preferred_ssid']
            if preferred and preferred in available:
                candidates = [preferred] + [s for s in candidates if s != preferred]
            pending = [ssid for ssid in candidates if ssid not in attempted]
            if candidates and not pending:
                attempted.clear()
                pending = candidates
            for ssid in pending:
                remaining = int(deadline - time.monotonic())
                if not ap and remaining < 17:
                    break
                attempted.add(ssid)
                if join(ssid, cfg, 40 if ap else min(40, remaining - 7)):
                    ap = False
                    connected_settings = settings
                    deadline = time.monotonic() + 60
                    break
                if ap:
                    hotspot()
            if ap and active() != 'ascent-hotspot' and active() != 'ascent-client':
                hotspot()
            if ap:
                CHANGED.wait(HOTSPOT_RETRY_SECONDS)
                CHANGED.clear()
            else:
                time.sleep(2)
        except Exception as exc:
            print('Network controller error: ' + repr(exc), flush=True)
            update(network_error=str(exc))
            if ap:
                try:
                    if not (active() == 'ascent-client' and connected()):
                        hotspot()
                except Exception as recovery_error:
                    update(network_error='Hotspot recovery failed: ' + str(recovery_error))
                CHANGED.wait(HOTSPOT_RETRY_SECONDS)
                CHANGED.clear()
            else:
                time.sleep(2)


def idle_player(cfg, kind):
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    pipeline = Gst.parse_launch(
        'filesrc name=still ! pngdec ! videoscale ! videoconvert '
        '! video/x-raw,width=480,height=320,format=I420,pixel-aspect-ratio=1/1 '
        '! imagefreeze is-live=true ! video/x-raw,framerate=2/1 '
        '! textoverlay name=version-label valignment=bottom halignment=right '
        'font-desc="Sans 10" shaded-background=true xpad=5 ypad=4 '
        '! videoflip video-direction=' + cfg['rotation'] + ' ! videoconvert '
        '! video/x-raw,format=RGB16 ! kmssink driver-name=vc4 force-modesetting=true sync=false')
    pipeline.get_by_name('still').set_property('location', '/opt/ascent-viewer/' + kind + '.png')
    pipeline.get_by_name('version-label').set_property('text', 'v' + VERSION + ' / ' + BUILD)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(
            Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message.type == Gst.MessageType.ERROR:
            print(message.parse_error(), flush=True)
        return 1
    finally:
        pipeline.set_state(Gst.State.NULL)


def render_pipeline(cfg, legacy_render=False):
    if legacy_render:
        queue = 'queue max-size-buffers=1 max-size-time=0 max-size-bytes=0 leaky=downstream ! '
        stages = ('v4l2convert name=hwscale output-io-mode=dmabuf-import ! video/x-raw,width=480,height=270,pixel-aspect-ratio=1/1,format=I420 '
                  '! videoconvert n-threads=2 ! video/x-raw,format=YV12 ! ' + queue +
                  'videobox top=-25 bottom=-25 ! ' + queue +
                  'videoflip video-direction=' + cfg['rotation'] + ' ! ' + queue +
                  'videoconvert n-threads=2 ! video/x-raw,format=RGB16 ')
    else:
        stages = ('videoscale add-borders=true ! video/x-raw,width=480,height=320,pixel-aspect-ratio=1/1 ! '
                  'videoconvert ! video/x-raw,format=I420 '
                  '! videoflip video-direction=' + cfg['rotation'] + ' '
                  '! videoconvert ! video/x-raw,format=RGB16 ')
    return (stages + '! valve name=gate drop=false '
            '! kmssink name=display driver-name=vc4 force-modesetting=true sync=false '
            'enable-last-sample=false processing-deadline=0')


def legacy_render_action(color, width, height, current, allowed=True):
    if not ENABLE_LEGACY_RENDERER:
        return None
    supported = color == '1:4:16:4' and width == 1920 and height == 1080
    if supported and not current and allowed:
        return 5
    if not supported and current:
        return 6
    return None


def player(cfg, test=False, managed=False, legacy_receiver=False, legacy_render=False, allow_legacy_render=True, early_drop=True):
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    if managed and legacy_render:
        # Initialize DMA/KMS together after the metadata probe and splash exit.
        # Late sink activation can deadlock allocation negotiation.
        (RUN / 'display-ready').touch()
        deadline = time.monotonic()+10
        while not (RUN / 'display-permit').exists():
            if time.monotonic() >= deadline:
                (RUN / 'playback-error.txt').write_text('Hardware display handoff timed out')
                return 7
            time.sleep(.05)
        managed = False
    if test:
        prefix = 'videotestsrc is-live=true pattern=smpte ! '
    elif legacy_receiver:
        prefix = ('appsrc name=source is-live=true format=time do-timestamp=true '
                  'caps="video/x-h264,stream-format=(string)byte-stream" '
                  '! h264parse name=parser ! capssetter name=compatibility '
                  '! v4l2h264dec name=decoder '
                  '! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! ')
    else:
        prefix = ('rtspsrc name=source ! application/x-rtp,media=video,encoding-name=H264 '
                  '! rtph264depay name=depay ! h264parse name=parser '
                  '! capssetter name=compatibility ! v4l2h264dec name=decoder '
                  '! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! ')
    if legacy_render:
        prefix = prefix.replace('v4l2h264dec name=decoder', 'v4l2h264dec name=decoder capture-io-mode=dmabuf')
    description = prefix + render_pipeline(cfg, legacy_render)
    try:
        pipe = Gst.parse_launch(description)
    except Exception as exc:
        if not legacy_render:
            raise
        (RUN / 'playback-error.txt').write_text('Hardware renderer setup failed: '+str(exc))
        print('Hardware renderer setup failed:', exc, flush=True)
        return 7
    receiver = None
    if not test and not legacy_receiver:
        source = pipe.get_by_name('source')
        source.set_property('location', cfg['rtsp_url'])
        source.set_property('latency', cfg['latency_ms'])
        source.set_property('drop-on-latency', True)
        source.set_property('protocols', 4 if cfg['transport'] == 'tcp' else 1)
        source.set_property('tcp-timeout', 5000000)
    elif legacy_receiver:
        from legacy_receiver import Receiver
        receiver = Receiver(cfg['rtsp_url'], pipe.get_by_name('source'))
    sink = pipe.get_by_name('display')
    gate = pipe.get_by_name('gate')
    display_active = not managed
    if managed:
        sink.set_locked_state(True)
        gate.set_property('drop', True)
        hint = [False]

        def display_ready(pad, info):
            if not hint[0]:
                (RUN / 'display-ready').touch()
                hint[0] = True
            return Gst.PadProbeReturn.OK

        gate.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, display_ready)
    transport_retry = [None]
    if not test:
        ready = [False]

        def compatible_color(pad, info):
            event = info.get_event()
            if event.type == Gst.EventType.CAPS:
                color = event.parse_caps().get_structure(0).get_string('colorimetry')
                replacement = compatible_colorimetry(color)
                if ready[0]:
                    if color:
                        transport_retry[0] = compatibility_transport_action(cfg['transport'], color, legacy_receiver)
                    if not legacy_receiver:
                        structure = event.parse_caps().get_structure(0)
                        render_action = legacy_render_action(color, structure.get_value('width'),
                                                             structure.get_value('height'), legacy_render, allow_legacy_render)
                        if render_action is not None:
                            transport_retry[0] = render_action
                caps = (Gst.Caps.from_string('video/x-h264,colorimetry=(string)' + replacement)
                        if replacement else Gst.Caps.new_any())
                pipe.get_by_name('compatibility').set_property('caps', caps)
                if replacement:
                    print(f'VRX color compatibility: {color} -> {replacement}', flush=True)
            return Gst.PadProbeReturn.OK

        pipe.get_by_name('compatibility').get_static_pad('sink').add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, compatible_color)

        def valid_headers(pad, info):
            if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
                event = info.get_event()
                if event.type == Gst.EventType.CAPS:
                    structure = event.parse_caps().get_structure(0)
                    valid_w, width = structure.get_int('width')
                    valid_h, height = structure.get_int('height')
                    ready[0] = bool(valid_w and valid_h and width > 0 and height > 0)
                    if not ready[0]:
                        return Gst.PadProbeReturn.DROP
            elif info.type & Gst.PadProbeType.BUFFER and not ready[0]:
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        pipe.get_by_name('parser').get_static_pad('src').add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.BUFFER, valid_headers)

        def read_metadata(info):
            event = info.get_event()
            if event.type == Gst.EventType.CAPS:
                structure = event.parse_caps().get_structure(0)
                data = {'codec_name': 'h264', 'transport': 'udp' if legacy_receiver else cfg['transport'],
                        'receiver': 'buffered-udp' if legacy_receiver else 'gstreamer',
                        'renderer': 'legacy-hardware' if legacy_render else 'standard'}
                for key in ('width', 'height'):
                    valid, value = structure.get_int(key)
                    if valid:
                        data[key] = value
                if structure.has_field('profile'):
                    data['profile'] = structure.get_string('profile')
                if structure.has_field('colorimetry'):
                    color = structure.get_string('colorimetry')
                    data['colorimetry'] = color
                    data['decoder_caps_workaround'] = compatible_colorimetry(color)
                if structure.has_field('framerate'):
                    valid, numerator, denominator = structure.get_fraction('framerate')
                    if valid:
                        data['framerate'] = f'{numerator}/{denominator}'
                (RUN / 'source.json').write_text(json.dumps(data))
        def metadata(pad, info):
            try:
                read_metadata(info)
            except Exception as exc:
                print('Metadata read failed:', exc, flush=True)
            return Gst.PadProbeReturn.OK
        pipe.get_by_name('parser').get_static_pad('src').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, metadata)
    last = [time.monotonic()]
    count = [0]
    metrics = dict(rtp_packets=0, compressed_buffers=0, compressed_bytes=0, decoded_frames=0,
                   rtp_gap_events=0, rtp_missing_packets=0)
    jitterbuffers = []
    sequence = {}
    rate_sample = [time.monotonic(), 0, 0]

    if not test:
        def new_manager(source, manager):
            def new_jitterbuffer(manager, buffer, session, ssrc):
                buffer.set_property('post-drop-messages', True)
                jitterbuffers.append(buffer)
            manager.connect('new-jitterbuffer', new_jitterbuffer)
        if not legacy_receiver:
            source.connect('new-manager', new_manager)

        def rtp_buffer(pad, info):
            if info.type & Gst.PadProbeType.BUFFER_LIST:
                buffers = info.get_buffer_list()
                batch = [buffers.get(i) for i in range(buffers.length())]
            else:
                batch = [info.get_buffer()]
            for buffer in batch:
                observe_rtp(metrics, sequence, buffer.extract_dup(0, 12))
            return Gst.PadProbeReturn.OK

        def compressed_buffer(pad, info):
            metrics['compressed_buffers'] += 1
            metrics['compressed_bytes'] += info.get_buffer().get_size()
            metrics['compressed_pts'] = info.get_buffer().pts / Gst.SECOND
            return Gst.PadProbeReturn.OK

        def decoded_buffer(pad, info):
            metrics['decoded_frames'] += 1
            metrics['decoded_pts'] = info.get_buffer().pts / Gst.SECOND
            return Gst.PadProbeReturn.OK

        if not legacy_receiver:
            depay = pipe.get_by_name('depay')
            depay.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, rtp_buffer)
            depay.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, compressed_buffer)
        else:
            pipe.get_by_name('parser').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, compressed_buffer)
        pipe.get_by_name('decoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, decoded_buffer)

    if legacy_render and early_drop:
        raw_budget = [1.0, time.monotonic()]
        def raw_frame(pad, info):
            now = time.monotonic()
            raw_budget[0] = min(2.0, raw_budget[0]+(now-raw_budget[1])*cfg['max_fps'])
            raw_budget[1] = now
            if raw_budget[0] < 1:
                metrics['raw_frames_skipped'] = metrics.get('raw_frames_skipped', 0)+1
                return Gst.PadProbeReturn.DROP
            raw_budget[0] -= 1
            return Gst.PadProbeReturn.OK
        pipe.get_by_name('hwscale').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, raw_frame)

    frame_budget = [1.0, time.monotonic()]

    def frame(pad, info):
        now = time.monotonic()
        frame_budget[0] = min(2.0, frame_budget[0] + (now-frame_budget[1]) * cfg['max_fps'])
        frame_budget[1] = now
        if frame_budget[0] < 1:
            return Gst.PadProbeReturn.DROP
        frame_budget[0] -= 1
        last[0] = now
        count[0] += 1
        metrics['last_frame_at'] = time.time()
        metrics['display_pts'] = info.get_buffer().pts / Gst.SECOND
        if count[0] % 15 == 0:
            (RUN / 'frames.json').write_text(json.dumps(dict(frames=count[0], at=time.time())))
        return Gst.PadProbeReturn.OK

    sink.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, frame)
    bus = pipe.get_bus()
    pipe.set_state(Gst.State.PLAYING)
    if receiver:
        receiver.start()
    try:
        while True:
            if transport_retry[0] is not None:
                print('Stream pipeline restart:', transport_retry[0], flush=True)
                return transport_retry[0]
            poll_time = 100 * Gst.MSECOND if managed and not display_active else Gst.SECOND
            msg = bus.timed_pop_filtered(poll_time, Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT)
            if msg and msg.type == Gst.MessageType.ELEMENT:
                report = msg.get_structure()
                if report and report.get_name() == 'drop-msg':
                    for field in ('num-drop-on-latency', 'num-too-late'):
                        key = 'jitter_reported_'+field.replace('-', '_')
                        metrics[key] = metrics.get(key, 0)+int(report.get_value(field) or 0)
                    metrics['jitter_last_drop_reason'] = report.get_string('reason')
            if managed and not display_active and (RUN / 'display-permit').exists():
                sink.set_locked_state(False)
                if not sink.sync_state_with_parent():
                    print('KMS display activation failed', flush=True)
                    return 1
                gate.set_property('drop', False)
                display_active = True
                last[0] = time.monotonic()
            for buffer in jitterbuffers:
                stats = buffer.get_property('stats')
                metrics['jitterbuffer'] = {key: stats.get_value(key) for key in
                                           ('num-pushed', 'num-lost', 'num-late', 'num-duplicates', 'avg-jitter')}
            if receiver:
                metrics.update(receiver.metrics())
                if receiver.error:
                    (RUN / 'playback-error.txt').write_text(receiver.error)
                    print('Older VRX receiver:', receiver.error, flush=True)
                    return 1
            now = time.monotonic()
            elapsed = now-rate_sample[0]
            if elapsed >= 1:
                metrics['decode_fps'] = (metrics['decoded_frames']-rate_sample[1])/elapsed
                metrics['display_fps'] = (count[0]-rate_sample[2])/elapsed
                rate_sample[:] = [now, metrics['decoded_frames'], count[0]]
            metrics.update(playback_backlog(metrics))
            (RUN / 'metrics.json').write_text(json.dumps({**metrics, 'frames': count[0],
                                                         'at': time.time()}))
            if msg and msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
                if msg.type == Gst.MessageType.ERROR:
                    err, detail = msg.parse_error()
                    print(str(err), detail, flush=True)
                    (RUN / 'playback-error.txt').write_text(str(err))
                    source = pipe.get_by_name('source')
                    source_error = msg.src == source or (source is not None and msg.src.has_as_ancestor(source))
                    if legacy_render and not source_error:
                        return 7
                return 1
            if time.monotonic() - last[0] > cfg['watchdog_seconds']:
                print('No decoded frames: restarting playback', flush=True)
                (RUN / 'playback-error.txt').write_text('No decoded frames; reconnecting')
                return 2
    finally:
        if receiver:
            receiver.close()
        pipe.set_state(Gst.State.NULL)


def compatible_colorimetry(color):
    # Older VRX spectator60: this exact tuple is rejected by the Pi V4L2 caps.
    # A BT.709 interpretation was verified against the same captured H.264 data.
    return 'bt709' if color == '1:4:16:4' else None


def compatibility_transport_action(configured, color, legacy_receiver):
    if not EXPERIMENTAL_LEGACY_RECEIVER:
        return None
    needs_receiver = configured == 'udp' and compatible_colorimetry(color) is not None
    if needs_receiver and not legacy_receiver:
        return 3
    if not needs_receiver and legacy_receiver:
        return 4
    return None


def idle_kind(connection, has_address, setup_mode=False):
    if setup_mode:
        return 'setup'
    if has_address:
        if connection == 'ascent-hotspot':
            return 'hotspot'
        if connection in ('ascent-client', 'ascent-test'):
            return 'waiting-vtx'
    return 'waiting'


def playback_loop():
    standby = None
    standby_key = None
    legacy_receiver = False
    legacy_render = False
    legacy_render_failed = False
    previous_cfg = None

    def stop(process):
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def show_idle(cfg):
        nonlocal standby, standby_key
        key = (idle_kind(active(), connected(), cfg['setup_mode']), cfg['rotation'])
        if standby is None or standby.poll() is not None or standby_key != key:
            stop(standby)
            standby = subprocess.Popen([sys.executable, __file__, '--idle', key[0]])
            standby_key = key

    while True:
        cfg = read_config()
        if cfg != previous_cfg:
            legacy_receiver = False
            legacy_render = legacy_render_failed = False
            previous_cfg = cfg
        update(renderer_fallback=legacy_render_failed)
        show_idle(cfg)
        if cfg['setup_mode']:
            legacy_receiver = False
            legacy_render = legacy_render_failed = False
            update(playback='setup mode')
            time.sleep(2)
            continue
        connection = active()
        if connection == 'ascent-hotspot':
            legacy_receiver = False
            legacy_render = legacy_render_failed = False
            update(playback='waiting for a client network')
            time.sleep(3)
            continue
        if connection not in ('ascent-client', 'ascent-test') or not connected():
            legacy_receiver = False
            legacy_render = legacy_render_failed = False
            update(playback='waiting for VRX')
            time.sleep(2)
            continue
        (RUN / 'frames.json').unlink(missing_ok=True)
        (RUN / 'playback-error.txt').unlink(missing_ok=True)
        (RUN / 'display-ready').unlink(missing_ok=True)
        (RUN / 'display-permit').unlink(missing_ok=True)
        update(playback='waiting for VTX')
        args = [sys.executable, __file__, '--player', '--managed-display']
        if legacy_receiver:
            args.append('--legacy-receiver')
        if legacy_render:
            args.append('--legacy-render')
        if legacy_render_failed:
            args.append('--no-legacy-render')
        child = subprocess.Popen(args)
        started = time.monotonic()
        try:
            while child.poll() is None:
                if cfg != read_config() or active() == 'ascent-hotspot' or RECONNECT.is_set():
                    legacy_receiver = False
                    legacy_render = legacy_render_failed = False
                    RECONNECT.clear()
                    break
                if active() not in ('ascent-client', 'ascent-test'):
                    break
                if (RUN / 'display-ready').exists() and not (RUN / 'display-permit').exists():
                    stop(standby)
                    standby = None
                    (RUN / 'display-permit').touch()
                framefile = RUN / 'frames.json'
                if framefile.exists():
                    try:
                        frames = json.loads(framefile.read_text())
                        update(playback='playing' if time.time() - frames['at'] < 3 else 'waiting for frames',
                               frames=frames['frames'], playback_error='')
                    except (ValueError, OSError):
                        pass
                errorfile = RUN / 'playback-error.txt'
                if errorfile.exists():
                    update(playback_error=errorfile.read_text())
                sourcefile = RUN / 'source.json'
                if sourcefile.exists():
                    try:
                        update(source=json.loads(sourcefile.read_text()))
                    except (ValueError, OSError):
                        pass
                if time.monotonic() - started > 25 and not framefile.exists():
                    break
                time.sleep(1)
        finally:
            stop(child)
            errorfile = RUN / 'playback-error.txt'
            if errorfile.exists():
                update(playback_error=errorfile.read_text())
        if child.returncode in (3, 4):
            legacy_receiver = child.returncode == 3
            update(playback='selecting stream transport')
            continue
        if child.returncode in (5, 6):
            legacy_render = child.returncode == 5
            update(playback='selecting stream renderer')
            continue
        if legacy_render and child.returncode == 7:
            legacy_render = False
            legacy_render_failed = True
            update(playback_error='Hardware rendering stopped; retrying the standard renderer')
            print('Hardware rendering stopped; retrying the standard renderer', flush=True)
        elif legacy_render:
            # Re-probe before acquiring KMS again, preserving the VTX waiting
            # splash while the RTSP server is unavailable.
            legacy_render = False
        update(playback='retrying')
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def authorized(self):
        expected = 'Basic ' + base64.b64encode(b'streamer:streamer').decode()
        if hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Streamer"')
        self.end_headers()
        return False

    def send(self, code, data, kind='text/plain'):
        self.send_response(code)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data.encode())

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == '/':
            self.send(200, Path(__file__).with_name('web').joinpath('index.html').read_text(), 'text/html')
        elif self.path == '/api':
            cfg = read_config()
            cfg['network_password'] = ''
            with LOCK:
                status = copy.deepcopy(STATUS)
            status['metrics'] = runtime_metrics()
            self.send(200, json.dumps(dict(config=cfg, status=status, token=TOKEN, version=VERSION, build=BUILD)), 'application/json')
        else:
            self.send(404, 'Not found')

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 8192:
                raise ValueError('Invalid request length')
            body = self.rfile.read(length)
        except (ValueError, OSError):
            self.send(400, 'Invalid request body')
            return
        if not self.authorized():
            return
        if self.path not in ('/api', '/api/reconnect') or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), TOKEN):
            self.send(403, 'Invalid request')
            return
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('Settings must be a JSON object')
            if self.path == '/api/reconnect':
                RECONNECT.set()
                self.send(200, 'Reconnecting video.')
                return
            cfg = read_config()
            for key in DEFAULT:
                if key not in data:
                    continue
                value = data[key]
                if key == 'network_password' and not value:
                    continue
                if key == 'setup_mode':
                    if str(value).lower() not in ('true', 'false'):
                        raise ValueError('Invalid mode')
                    value = str(value).lower() == 'true'
                elif key in ('latency_ms', 'max_fps', 'watchdog_seconds'):
                    value = int(value)
                else:
                    value = str(value)
                cfg[key] = value
            if cfg['rotation'] not in ('90r', '90l') or cfg['transport'] not in ('tcp', 'udp'):
                raise ValueError('Invalid playback setting')
            if cfg['scaler'] != 'software':
                raise ValueError('This release uses the verified software scaler')
            if not (0 <= cfg['latency_ms'] <= 1000 and 1 <= cfg['max_fps'] <= 60 and 5 <= cfg['watchdog_seconds'] <= 60):
                raise ValueError('Playback value out of range')
            if len(cfg['preferred_ssid'].encode()) > 32 or not 8 <= len(cfg['network_password']) <= 63:
                raise ValueError('Invalid SSID or password length')
            if cfg['static_address']:
                ipaddress.IPv4Interface(cfg['static_address'])
            if urlsplit(cfg['rtsp_url']).scheme != 'rtsp' or not urlsplit(cfg['rtsp_url']).hostname:
                raise ValueError('An rtsp:// URL is required')
            write_config(cfg)
            CHANGED.set()
            self.send(200, 'Saved. Playback will restart; network mode changes may disconnect this page.')
        except (ValueError, TypeError, OSError) as exc:
            self.send(400, str(exc))


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        write_config(DEFAULT)
    if '--idle' in sys.argv:
        return idle_player(read_config(), sys.argv[sys.argv.index('--idle') + 1])
    if '--player' in sys.argv or '--test' in sys.argv:
        return player(read_config(), '--test' in sys.argv, '--managed-display' in sys.argv,
                      '--legacy-receiver' in sys.argv, '--legacy-render' in sys.argv,
                      '--no-legacy-render' not in sys.argv)
    def worker(target):
        try:
            target()
        except Exception:
            traceback.print_exc()
            os._exit(1)  # systemd restarts the entire appliance, including children.
    threading.Thread(target=worker, args=(network_loop,), daemon=True).start()
    threading.Thread(target=worker, args=(playback_loop,), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__':
    sys.exit(main())
