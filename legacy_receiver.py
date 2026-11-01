"""Buffered receiver for the observed older VRX H.264 stream."""
import socket
import struct
import threading

from capture_rtsp import Capture


class KeyframeGate:
    """Keep complete NALs and resume at an IDR after missing RTP data."""
    def __init__(self, send):
        self.send = send
        self.waiting = True
        self.headers = {}
        self.dropped = 0
        self.recoveries = 0

    def lost(self):
        self.waiting = True

    def write(self, data):
        kind = data[4] & 31
        if kind in (7, 8):
            self.headers[kind] = data
            self.send(data)
        elif kind == 5:
            if self.waiting:
                for header in (7, 8):
                    if header in self.headers:
                        self.send(self.headers[header])
                self.recoveries += 1
            self.waiting = False
            self.send(data)
        elif not self.waiting:
            self.send(data)
        else:
            self.dropped += 1


class GuardedCapture(Capture):
    def rtp(self, data):
        if len(data) >= 12:
            seq = struct.unpack_from('!H', data, 2)[0]
            ssrc = struct.unpack_from('!I', data, 8)[0]
            previous_ssrc = getattr(self, '_ssrc', ssrc)
            if ((self.last_sequence is not None and seq != ((self.last_sequence + 1) & 65535))
                    or ssrc != previous_ssrc):
                self.output.lost()
                self.fragment = None
                self.last_fragment_sequence = None
            self._ssrc = ssrc
        super().rtp(data)


class Receiver:
    def __init__(self, url, appsrc):
        self.url = url
        self.appsrc = appsrc
        self.stop_event = threading.Event()
        self.error = None
        self.capture = None
        self.gate = KeyframeGate(self.push)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def push(self, data):
        from gi.repository import Gst
        if self.stop_event.is_set():
            return
        if self.appsrc.get_property('current-level-bytes') > 8 * 1024 * 1024:
            raise RuntimeError('Older VRX decode backlog exceeded 8 MiB; restarting at a keyframe')
        buffer = Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        result = self.appsrc.emit('push-buffer', buffer)
        if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
            raise RuntimeError('Older VRX input stopped: ' + str(result))

    def run(self):
        try:
            capture = GuardedCapture(self.url, 'udp', self.gate, udp_receive_buffer=8 * 1024 * 1024)
            self.capture = capture
            capture.run(float('inf'), self.stop_event)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = str(exc)
        finally:
            if not self.stop_event.is_set():
                self.appsrc.emit('end-of-stream')

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.capture:
            try:
                self.capture.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=2)

    def metrics(self):
        stats = dict(self.capture.stats) if self.capture else {}
        return dict(receiver='buffered-udp', rtp_packets=stats.get('rtp_packets', 0),
                    rtp_gap_events=stats.get('sequence_gaps', 0),
                    incomplete_nals=stats.get('broken_fragments', 0),
                    keyframe_recoveries=self.gate.recoveries,
                    dropped_until_keyframe=self.gate.dropped)
