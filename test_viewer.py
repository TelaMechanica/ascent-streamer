import base64
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import struct
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import viewer


class StopLoop(BaseException):
    pass


class PlaybackMetricsTests(unittest.TestCase):
    def test_packet_wrap_duplicate_reordering_and_source_change_are_distinct(self):
        metrics = dict(rtp_packets=0, rtp_gap_events=0, rtp_missing_packets=0)
        state = {}
        for seq, ssrc in [(65534, 10), (65535, 10), (0, 10), (0, 10), (65535, 10),
                          (1, 10), (4, 10), (1000, 20), (1001, 20)]:
            viewer.observe_rtp(metrics, state, struct.pack('!BBHII', 128, 96, seq, 0, ssrc))
        self.assertEqual(metrics['rtp_missing_packets'], 2)
        self.assertEqual(metrics['rtp_gap_events'], 1)
        self.assertEqual(metrics['rtp_duplicates'], 1)
        self.assertEqual(metrics['rtp_reordered'], 1)
        self.assertEqual(metrics['rtp_ssrc_changes'], 1)

    def test_backlog_is_pts_distance_and_rejects_missing_or_invalid_timestamps(self):
        self.assertEqual(viewer.playback_backlog({}), {})
        self.assertEqual(viewer.playback_backlog(dict(compressed_pts=12, decoded_pts=10, display_pts=9)),
                         dict(decode_backlog_ms=2000, render_backlog_ms=1000))
        for invalid in (None, float('nan'), float('inf'), -1, 18446744073.7):
            self.assertEqual(viewer.playback_backlog(dict(compressed_pts=invalid, decoded_pts=1)), {})

    def test_runtime_sample_carries_age_and_tolerates_partial_write(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(viewer, 'RUN', Path(directory)):
            sample = Path(directory)/'metrics.json'
            self.assertIsNone(viewer.runtime_metrics())
            sample.write_text('{"at":100,"frames":20}')
            with patch.object(viewer.time, 'time', return_value=105):
                self.assertEqual(viewer.runtime_metrics()['sample_age_seconds'], 5)
            sample.write_text('{"at":')
            self.assertIsNone(viewer.runtime_metrics())


class RendererTests(unittest.TestCase):
    def test_metadata_selects_only_the_qualified_geometry_and_respects_fallback(self):
        with patch.object(viewer, 'ENABLE_LEGACY_RENDERER', True):
            self.assertEqual(viewer.legacy_render_action('1:4:16:4', 1920, 1080, False), 5)
            self.assertIsNone(viewer.legacy_render_action('1:4:16:4', 1920, 1080, True))
            self.assertIsNone(viewer.legacy_render_action('1:4:16:4', 1920, 1080, False, allowed=False))
            for color, width, height in [('bt709',1920,1080),(None,1920,1080),('1:4:16:4',1280,720),('1:4:16:4',640,480)]:
                self.assertIsNone(viewer.legacy_render_action(color,width,height,False))
                self.assertEqual(viewer.legacy_render_action(color,width,height,True),6)

    def test_standard_pipeline_retains_approved_scaling_and_output(self):
        expected = ('videoscale add-borders=true ! video/x-raw,width=480,height=320,pixel-aspect-ratio=1/1 ! '
                    'videoconvert ! video/x-raw,format=I420 ! videoflip video-direction=90r '
                    '! videoconvert ! video/x-raw,format=RGB16 ! valve name=gate drop=false '
                    '! kmssink name=display driver-name=vc4 force-modesetting=true sync=false '
                    'enable-last-sample=false processing-deadline=0')
        self.assertEqual(viewer.render_pipeline(viewer.DEFAULT),expected)

    def run_controller(self, exits):
        players=[]
        class Process:
            def __init__(self,code): self.returncode=code
            def poll(self): return self.returncode
            def terminate(self): pass
            def wait(self,timeout=None): return self.returncode
        def spawn(args):
            if '--idle' in args: return Process(None)
            players.append(args)
            if len(players)>len(exits): raise StopLoop()
            return Process(exits[len(players)-1])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(viewer,'RUN',Path(directory)), \
             patch.object(viewer,'read_config',return_value=viewer.DEFAULT.copy()), \
             patch.object(viewer,'active',return_value='ascent-client'), \
             patch.object(viewer,'connected',return_value=True), \
             patch.object(viewer.subprocess,'Popen',side_effect=spawn), \
             patch.object(viewer.time,'sleep'), patch.dict(viewer.STATUS,{},clear=True):
            with self.assertRaises(StopLoop): viewer.playback_loop()
        return players

    def test_failed_hardware_renderer_falls_back_once_without_upgrade_loop(self):
        players=self.run_controller([5,7])
        self.assertIn('--legacy-render',players[1])
        self.assertNotIn('--legacy-render',players[2])
        self.assertIn('--no-legacy-render',players[2])

    def test_vtx_watchdog_reprobes_before_reacquiring_display(self):
        players=self.run_controller([5,2,5])
        self.assertNotIn('--legacy-render',players[2])
        self.assertNotIn('--no-legacy-render',players[2])
        self.assertIn('--legacy-render',players[3])

    def test_new_max_stream_returns_to_standard_renderer(self):
        players=self.run_controller([5,6])
        self.assertNotIn('--legacy-render',players[2])


class IdleScreenTests(unittest.TestCase):
    def test_transport_selection_uses_stream_metadata_not_ssid(self):
        with patch.object(viewer, 'EXPERIMENTAL_LEGACY_RECEIVER', True):
            self.assertEqual(viewer.compatibility_transport_action('udp', '1:4:16:4', False), 3)
            self.assertIsNone(viewer.compatibility_transport_action('udp', '1:4:16:4', True))
            self.assertEqual(viewer.compatibility_transport_action('udp', 'bt709', True), 4)
            self.assertIsNone(viewer.compatibility_transport_action('udp', 'bt709', False))
            self.assertIsNone(viewer.compatibility_transport_action('tcp', 'bt709', False))
            self.assertIsNone(viewer.compatibility_transport_action('tcp', '1:4:16:4', False))

    def test_failed_receiver_is_not_selected_automatically(self):
        self.assertFalse(viewer.EXPERIMENTAL_LEGACY_RECEIVER)
        self.assertIsNone(viewer.compatibility_transport_action('udp', '1:4:16:4', False))

    def test_color_workaround_is_limited_to_observed_old_vrx_tuple(self):
        self.assertEqual(viewer.compatible_colorimetry('1:4:16:4'), 'bt709')
        for color in (None, '', 'bt709', 'bt601', '2:4:5:1', 'bt2020'):
            with self.subTest(color=color):
                self.assertIsNone(viewer.compatible_colorimetry(color))

    def test_setup_mode_takes_priority_during_network_transition(self):
        for connection in ('preconfigured', 'ascent-client', 'ascent-hotspot', ''):
            for ready in (True, False):
                with self.subTest(connection=connection, ready=ready):
                    self.assertEqual(viewer.idle_kind(connection, ready, True), 'setup')

    def test_screen_tracks_network_readiness(self):
        for connection, ready, expected in (
            ('ascent-client', True, 'waiting-vtx'),
            ('ascent-test', True, 'waiting-vtx'),
            ('ascent-client', False, 'waiting'),
            ('ascent-hotspot', True, 'hotspot'),
            ('ascent-hotspot', False, 'waiting'),
            ('preconfigured', True, 'waiting'),
            ('', False, 'waiting'),
        ):
            with self.subTest(connection=connection, ready=ready):
                self.assertEqual(viewer.idle_kind(connection, ready), expected)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.status = patch.dict(viewer.STATUS, {}, clear=True)
        self.status.start()
        timestamps = patch.object(viewer, "scan_timestamp", side_effect=__import__("itertools").count())
        timestamps.start()
        self.addCleanup(timestamps.stop)
        self.addCleanup(self.status.stop)

    def test_no_network_starts_hotspot_at_sixty_seconds(self):
        clock = [0]
        fallback = []

        def sleep(seconds):
            clock[0] += seconds

        def ap():
            fallback.append(clock[0])
            raise StopLoop()

        cfg = {**viewer.DEFAULT, 'setup_mode': False}
        with patch.object(viewer, 'read_config', return_value=cfg), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan', return_value=[]), \
             patch.object(viewer, 'hotspot', side_effect=ap), \
             patch.object(viewer.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(viewer.time, 'sleep', side_effect=sleep):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        self.assertEqual(fallback, [60])

    def test_fallback_holds_hotspot_before_retrying_scan(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(viewer, 'read_config', return_value=viewer.DEFAULT.copy()), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan', return_value=[]), \
             patch.object(viewer, 'hotspot'), \
             patch.object(viewer.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(viewer.time, 'sleep', side_effect=sleep), \
             patch.object(viewer.CHANGED, 'wait', side_effect=StopLoop) as wait:
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        wait.assert_called_once_with(120)

    def test_preferred_ssid_before_other_substring_matches(self):
        cfg = {**viewer.DEFAULT, 'setup_mode': False, 'preferred_ssid': 'xASCENT-y'}
        with patch.object(viewer, 'read_config', return_value=cfg), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan', return_value=['Other', 'ASCENT-A', 'xASCENT-y']), \
             patch.object(viewer, 'join', return_value=True) as join, \
             patch.object(viewer.time, 'sleep', side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        self.assertEqual(join.call_args.args[0], 'xASCENT-y')

    def test_any_prefix_and_suffix_match(self):
        cfg = {**viewer.DEFAULT, 'setup_mode': False}
        with patch.object(viewer, 'read_config', return_value=cfg), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan', return_value=['Other', 'prefixASCENTsuffix']), \
             patch.object(viewer, 'join', return_value=True) as join, \
             patch.object(viewer.time, 'sleep', side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        self.assertEqual(join.call_args.args[0], 'prefixASCENTsuffix')

    def test_fresh_configuration_is_automatic(self):
        self.assertFalse(viewer.DEFAULT['setup_mode'])

    def test_exact_reported_ssid_and_prefix_suffix_variants(self):
        for ssid in ('ASCENT-example', '_ASCENT_', 'prefix-ASCENT-example-suffix', 'ASCENT'):
            with self.subTest(ssid=ssid), \
                 patch.object(viewer, 'read_config', return_value=viewer.DEFAULT.copy()), \
                 patch.object(viewer, 'active', return_value='preconfigured'), \
                 patch.object(viewer, 'scan', return_value=['unrelated', ssid]), \
                 patch.object(viewer, 'join', return_value=True) as join, \
                 patch.object(viewer.time, 'sleep', side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    viewer.network_loop()
                self.assertEqual(join.call_args.args[0], ssid)

    def test_hung_first_candidate_does_not_starve_second(self):
        clock = [0]
        attempts = []

        def join(ssid, config, timeout):
            attempts.append((ssid, timeout))
            if ssid == 'ASCENT-old':
                clock[0] += 44
                raise subprocess.TimeoutExpired('nmcli', timeout)
            return True

        def sleep(seconds):
            if len(attempts) > 1:
                raise StopLoop()
            clock[0] += seconds

        with patch.object(viewer, 'read_config', return_value=viewer.DEFAULT.copy()), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan', return_value=['ASCENT-old', 'ASCENT-example']), \
             patch.object(viewer, 'join', side_effect=join), \
             patch.object(viewer, 'hotspot'), \
             patch.object(viewer.CHANGED, 'wait', return_value=False), \
             patch.object(viewer.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(viewer.time, 'sleep', side_effect=sleep):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        self.assertEqual(attempts, [('ASCENT-old', 40), ('ASCENT-example', 40)])

    def test_join_passes_reported_ssid_and_password_verbatim(self):
        success = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(viewer, 'profiles', return_value=[]), \
             patch.object(viewer, 'command', return_value=success) as command:
            self.assertTrue(viewer.join('ASCENT-example', viewer.DEFAULT.copy(), 40))
        modifications = [c.args[0] for c in command.call_args_list if 'modify' in c.args[0]][0]
        self.assertEqual(modifications[modifications.index('802-11-wireless.ssid')+1], 'ASCENT-example')
        self.assertEqual(modifications[modifications.index('wifi-sec.psk')+1], '12345678')

    def test_hotspot_scan_resets_station_mode_before_scanning(self):
        success = subprocess.CompletedProcess([], 0, stdout='ASCENT-example\n', stderr='')
        ready = subprocess.CompletedProcess([], 0, stdout='30 (disconnected)\n', stderr='')
        with patch.object(viewer, 'active', return_value='ascent-hotspot'), \
             patch.object(viewer, 'hotspot_has_clients', return_value=False), \
             patch.object(viewer, 'command', side_effect=lambda args, *rest: ready if 'GENERAL.STATE' in args else success) as command:
            self.assertEqual(viewer.scan(), ['ASCENT-example'])
        calls = [c.args[0] for c in command.call_args_list]
        self.assertLess(calls.index(['nmcli', 'device', 'set', 'wlan0', 'managed', 'no']),
                        calls.index(['ip', 'link', 'set', 'wlan0', 'down']))
        self.assertLess(calls.index(['ip', 'link', 'set', 'wlan0', 'up']),
                        calls.index(['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes']))
        self.assertLess(calls.index(['ip', 'link', 'set', 'wlan0', 'down']),
                        calls.index(['iw', 'dev', 'wlan0', 'set', 'type', 'managed']))
        self.assertLess(calls.index(['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes']),
                        calls.index(['nmcli', '-g', 'SSID', 'device', 'wifi', 'list', 'ifname', 'wlan0', '--rescan', 'yes']))

    def test_hotspot_client_prevents_station_reset_and_scan(self):
        with patch.object(viewer, 'active', return_value='ascent-hotspot'), \
             patch.object(viewer, 'hotspot_has_clients', return_value=True), \
             patch.object(viewer, 'prepare_station') as reset, \
             patch.object(viewer, 'command') as command:
            self.assertIsNone(viewer.scan())
        reset.assert_not_called()
        command.assert_not_called()

    def test_radio_client_detection_and_failure_preserve_hotspot(self):
        for code, output, expected in [(0, 'Station <station-address> (on wlan0)\n', True),
                                        (0, '', False), (1, '', True)]:
            with self.subTest(code=code, output=output), \
                 patch.object(viewer, 'command', return_value=subprocess.CompletedProcess([], code, output, '')):
                self.assertEqual(viewer.hotspot_has_clients(), expected)
        with patch.object(viewer, 'command', side_effect=subprocess.TimeoutExpired('iw', 5)):
            self.assertTrue(viewer.hotspot_has_clients())

    def test_failed_scan_is_not_treated_as_no_networks(self):
        failed = subprocess.CompletedProcess([], 1, stdout='', stderr='scan failed')
        with patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'command', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'scan failed'):
                viewer.scan()

    def test_scan_reads_results_after_confirmed_completion(self):
        result = subprocess.CompletedProcess([], 0, stdout='ASCENT-example\n', stderr='')
        with patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'command', return_value=result) as command:
            self.assertEqual(viewer.scan(), ['ASCENT-example'])
        self.assertEqual(command.call_args_list[-1].args,
                         (['nmcli', '-g', 'SSID', 'device', 'wifi', 'list',
                           'ifname', 'wlan0', '--rescan', 'no'], 5))

    def test_stale_scan_results_are_rejected(self):
        success = subprocess.CompletedProcess([], 0, stdout='ASCENT-example\n', stderr='')
        with patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'scan_timestamp', return_value=100), \
             patch.object(viewer, 'command', return_value=success) as command, \
             patch.object(viewer.time, 'monotonic', side_effect=[0, 21]), \
             patch.object(viewer.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'cached results rejected'):
                viewer.scan()
        self.assertEqual(command.call_count, 1)

    def test_waits_for_station_readiness_after_device_recreation(self):
        unavailable = subprocess.CompletedProcess([], 0, stdout='20 (unavailable)\n', stderr='')
        ready = subprocess.CompletedProcess([], 0, stdout='30 (disconnected)\n', stderr='')
        with patch.object(viewer, 'command', side_effect=[unavailable, ready]) as command, \
             patch.object(viewer.time, 'sleep') as sleep:
            viewer.wait_station_ready()
        self.assertEqual(command.call_count, 2)
        sleep.assert_called_once_with(.25)

    def test_reset_failure_still_restores_networkmanager_ownership(self):
        success = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        failure = subprocess.CompletedProcess([], 1, stdout='', stderr='interface error')
        with patch.object(viewer, 'command', side_effect=[success, success, success, failure, success]) as command:
            with self.assertRaises(RuntimeError):
                viewer.prepare_station()
        self.assertEqual(command.call_args.args[0], ['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes'])

    def test_scanning_from_home_does_not_reset_radio(self):
        success = subprocess.CompletedProcess([], 0, stdout='ASCENT-example\n', stderr='')
        with patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'command', return_value=success) as command:
            viewer.scan()
        self.assertFalse(any('managed' in c.args[0] for c in command.call_args_list))

    def test_setup_return_leaves_hotspot_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'setup-network.uuid').write_text('saved-home-uuid')
            order = []
            def command(args, *rest):
                if 'up' in args:
                    order.append('home-up')
                return subprocess.CompletedProcess(args, 0, stdout='current-other-uuid', stderr='')
            with patch.object(viewer, 'ROOT', root), \
                 patch.object(viewer, 'active', return_value='ascent-hotspot'), \
                 patch.object(viewer, 'prepare_station', side_effect=lambda: order.append('station-reset')), \
                 patch.object(viewer, 'command', side_effect=command):
                viewer.restore_setup_network()
            self.assertEqual(order, ['station-reset', 'home-up'])

    def test_failed_setup_return_restores_configuration_hotspot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'setup-network.uuid').write_text('saved-home-uuid')
            results = [subprocess.CompletedProcess([], 0, stdout='', stderr=''),
                       subprocess.CompletedProcess([], 0, stdout='other', stderr=''),
                       subprocess.CompletedProcess([], 1, stdout='', stderr='not available')]
            with patch.object(viewer, 'ROOT', root), \
                 patch.object(viewer, 'active', return_value='ascent-hotspot'), \
                 patch.object(viewer, 'prepare_station'), \
                 patch.object(viewer, 'command', side_effect=results), \
                 patch.object(viewer, 'hotspot') as hotspot:
                viewer.restore_setup_network()
            hotspot.assert_called_once()

    def test_changed_credentials_force_a_new_join(self):
        first = {**viewer.DEFAULT, 'setup_mode': False}
        second = {**first, 'network_password': 'new-password'}
        configs = iter([first, second])
        sleeps = [0]

        def sleep(seconds):
            sleeps[0] += 1
            if sleeps[0] >= 2:
                raise StopLoop()

        with patch.object(viewer, 'read_config', side_effect=lambda: next(configs)), \
             patch.object(viewer, 'active', return_value='ascent-client'), \
             patch.object(viewer, 'connected', return_value=True), \
             patch.object(viewer, 'command') as command, \
             patch.object(viewer, 'scan', return_value=['ASCENT-A']), \
             patch.object(viewer, 'join', return_value=True) as join, \
             patch.object(viewer.time, 'sleep', side_effect=sleep):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        self.assertEqual(join.call_args.args[1]['network_password'], 'new-password')
        self.assertTrue(any('down' in call.args[0] for call in command.call_args_list))

    def test_setup_restore_happens_once_per_transition(self):
        cfg = {**viewer.DEFAULT, 'setup_mode': True}
        with patch.object(viewer, 'read_config', return_value=cfg), \
             patch.object(viewer, 'active', return_value='preconfigured'), \
             patch.object(viewer, 'restore_setup_network') as restore, \
             patch.object(viewer.CHANGED, 'wait', side_effect=[False, StopLoop]):
            with self.assertRaises(StopLoop):
                viewer.network_loop()
        restore.assert_called_once()


class WebTests(unittest.TestCase):
    def setUp(self):
        status = patch.dict(viewer.STATUS, {}, clear=True)
        status.start()
        self.addCleanup(status.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(viewer, 'ROOT', Path(self.temp.name))
        self.config = patch.object(viewer, 'CONFIG', Path(self.temp.name) / 'config.json')
        self.root.start()
        self.config.start()
        viewer.write_config(viewer.DEFAULT)
        self.server = viewer.ThreadingHTTPServer(('127.0.0.1', 0), viewer.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.config.stop()
        self.root.stop()
        self.temp.cleanup()

    def request(self, path='/api', data=None, token=True, auth=True):
        headers = {}
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'streamer:streamer').decode()
        if token:
            headers['X-CSRF-Token'] = viewer.TOKEN
        payload = json.dumps(data).encode() if data is not None else None
        return urlopen(Request(self.url + path, data=payload, headers=headers), timeout=3)

    def test_authentication_and_no_password_disclosure(self):
        with self.assertRaises(HTTPError) as result:
            self.request(auth=False)
        self.assertEqual(result.exception.code, 401)
        result.exception.close()
        with self.request() as response:
            self.assertEqual(json.load(response)['config']['network_password'], '')

    def test_status_exposes_current_diagnostics_with_sample_age(self):
        with patch.object(viewer, 'RUN', Path(self.temp.name)):
            (Path(self.temp.name)/'metrics.json').write_text(json.dumps(dict(at=100, frames=25)))
            with self.request() as response:
                data = json.load(response)
            self.assertEqual(data['status']['metrics']['frames'], 25)
            self.assertGreater(data['status']['metrics']['sample_age_seconds'], 0)
            self.assertEqual(data['build'], viewer.BUILD)

    def test_csrf_and_validation(self):
        with self.assertRaises(HTTPError) as result:
            self.request(data={'setup_mode': True}, token=False)
        self.assertEqual(result.exception.code, 403)
        result.exception.close()
        with self.assertRaises(HTTPError) as result:
            self.request(data={'rtsp_url': 'file:///etc/passwd'})
        self.assertEqual(result.exception.code, 400)
        result.exception.close()
        with self.assertRaises(HTTPError) as result:
            self.request(data={'static_address': 'not-an-address'})
        self.assertEqual(result.exception.code, 400)
        result.exception.close()
        self.assertFalse(viewer.read_config()['setup_mode'])

    def test_save_keeps_blank_password_and_persists_settings(self):
        with self.request(data={'latency_ms': 20, 'network_password': ''}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(viewer.read_config()['network_password'], '12345678')
        self.assertEqual(viewer.read_config()['latency_ms'], 20)

    def test_page_and_video_reconnect(self):
        with self.request(path='/') as response:
            self.assertIn('MAX playback preset selected locally', response.read().decode())
        viewer.RECONNECT.clear()
        with self.request(path='/api/reconnect', data={}) as response:
            self.assertEqual(response.status, 200)
        self.assertTrue(viewer.RECONNECT.is_set())
        viewer.RECONNECT.clear()

    def test_previous_config_recovers_corrupt_primary(self):
        viewer.write_config({**viewer.DEFAULT, 'latency_ms': 500})
        viewer.CONFIG.write_text('truncated')
        self.assertEqual(viewer.read_config()['latency_ms'], 200)
        viewer.write_config({**viewer.DEFAULT, 'latency_ms': 250})
        self.assertEqual(viewer.read_config()['latency_ms'], 250)
        self.assertEqual(json.loads(viewer.CONFIG.with_suffix('.previous.json').read_text())['latency_ms'], 200)


if __name__ == '__main__':
    unittest.main()
