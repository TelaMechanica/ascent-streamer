"""Sequential RTSP/RTP diagnostic capture, without a decoder or GStreamer."""
import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import re
import selectors
import socket
import struct
import time
from urllib.parse import urlsplit


class Capture:
    def __init__(self, url, transport, output, udp_receive_buffer=0):
        self.url = url
        self.transport = transport
        self.output = output
        self.udp_receive_buffer = udp_receive_buffer
        self.sock = socket.create_connection((urlsplit(url).hostname, urlsplit(url).port or 554), 5)
        self.sock.settimeout(5)
        self.buffer = b''
        self.cseq = 0
        self.session = None
        self.stats = Counter()
        self.nals = Counter()
        self.fragment = None
        self.last_sequence = None
        self.last_fragment_sequence = None
        self.sdp = ''

    def request(self, method, url=None, headers=None):
        self.cseq += 1
        fields = {'CSeq': str(self.cseq), 'User-Agent': 'AscentLocalDiagnostic/1'}
        if self.session:
            fields['Session'] = self.session
        fields.update(headers or {})
        request = f'{method} {url or self.url} RTSP/1.0\r\n'
        request += ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n'
        self.sock.sendall(request.encode())

    def response(self):
        while b'\r\n\r\n' not in self.buffer:
            self.buffer += self.receive()
        head, body = self.buffer.split(b'\r\n\r\n', 1)
        fields = {}
        for line in head.decode(errors='replace').split('\r\n')[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                fields[k.lower()] = v.strip()
        length = int(fields.get('content-length', '0'))
        while len(body) < length:
            body += self.receive()
        self.buffer = body[length:]
        return head.decode(errors='replace'), fields, body[:length]

    def receive(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError('RTSP server closed TCP connection')
        return data

    def nal(self, data):
        if data:
            self.output.write(b'\x00\x00\x00\x01' + data)
            self.nals[data[0] & 31] += 1

    def rtp(self, packet):
        if len(packet) < 12 or packet[0] >> 6 != 2:
            self.stats['invalid_rtp'] += 1
            return
        seq = struct.unpack_from('!H', packet, 2)[0]
        if self.last_sequence is not None and seq != ((self.last_sequence + 1) & 65535):
            self.stats['sequence_gaps'] += 1
        self.last_sequence = seq
        offset = 12 + (packet[0] & 15) * 4
        if packet[0] & 16:
            if len(packet) < offset + 4:
                return
            offset += 4 + struct.unpack_from('!H', packet, offset + 2)[0] * 4
        payload = packet[offset:len(packet) - packet[-1] if packet[0] & 32 else len(packet)]
        if not payload:
            return
        self.stats['rtp_packets'] += 1
        self.stats['rtp_payload_bytes'] += len(payload)
        kind = payload[0] & 31
        self.stats['payload_type_' + str(kind)] += 1
        if 1 <= kind <= 23:
            self.nal(payload)
        elif kind == 24:
            pos = 1
            while pos + 2 <= len(payload):
                size = struct.unpack_from('!H', payload, pos)[0]
                pos += 2
                if pos + size > len(payload):
                    self.stats['broken_stap'] += 1
                    break
                self.nal(payload[pos:pos + size])
                pos += size
        elif kind == 28 and len(payload) >= 2:
            if payload[1] & 128:
                self.fragment = bytearray([(payload[0] & 224) | (payload[1] & 31)])
                self.last_fragment_sequence = (seq - 1) & 65535
                self.stats['fragment_starts'] += 1
            if self.fragment is not None:
                if seq != ((self.last_fragment_sequence + 1) & 65535):
                    self.fragment = None
                    self.stats['broken_fragments'] += 1
                    return
                self.last_fragment_sequence = seq
                self.fragment.extend(payload[2:])
                if payload[1] & 64:
                    self.nal(self.fragment)
                    self.fragment = None
                    self.stats['fragment_ends'] += 1

    def run(self, seconds, stop_event=None):
        udp = []
        selector = selectors.DefaultSelector()
        try:
            self.request('OPTIONS')
            options = self.response()
            self.request('DESCRIBE', headers={'Accept': 'application/sdp'})
            head, fields, sdp = self.response()
            self.sdp = sdp.decode(errors='replace')
            base = fields.get('content-base', self.url.rstrip('/') + '/')
            controls = re.findall(r'^a=control:(.+)\r?$', self.sdp, re.M)
            control = next((x.strip() for x in controls if x.strip() != '*'), '')
            setup = control if control.startswith('rtsp://') else base + control
            if self.transport == 'udp':
                for port in (5000, 5001):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    if self.udp_receive_buffer:
                        try:
                            sock.setsockopt(socket.SOL_SOCKET, 33, self.udp_receive_buffer)  # Linux SO_RCVBUFFORCE
                        except OSError:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.udp_receive_buffer)
                        actual = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                        if actual < self.udp_receive_buffer:
                            sock.close()
                            raise RuntimeError(f'UDP receive buffer too small: {actual} bytes')
                    else:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2097152)
                    sock.bind(('0.0.0.0', port))
                    udp.append(sock)
                transport = 'RTP/AVP;unicast;client_port=5000-5001'
                selector.register(udp[0], selectors.EVENT_READ, 'rtp')
                selector.register(udp[1], selectors.EVENT_READ, 'rtcp')
            else:
                transport = 'RTP/AVP/TCP;unicast;interleaved=0-1'
            self.request('SETUP', setup, {'Transport': transport})
            setup_head, setup_fields, _ = self.response()
            self.session = setup_fields.get('session', '').split(';')[0]
            self.request('PLAY')
            play_head, _, _ = self.response()
            selector.register(self.sock, selectors.EVENT_READ, 'control')
            start = keepalive = time.monotonic()
            while time.monotonic() - start < seconds and not (stop_event and stop_event.is_set()):
                if time.monotonic() - keepalive > 4:
                    self.request('OPTIONS')
                    keepalive = time.monotonic()
                for event, _ in selector.select(0.25):
                    if event.data == 'rtp':
                        self.rtp(event.fileobj.recv(65536))
                    elif event.data == 'rtcp':
                        self.stats['rtcp_packets'] += 1
                        event.fileobj.recv(65536)
                    else:
                        self.buffer += self.receive()
                while self.buffer:
                    if self.buffer[0] == 36:
                        if len(self.buffer) < 4:
                            break
                        size = struct.unpack_from('!H', self.buffer, 2)[0]
                        if len(self.buffer) < size + 4:
                            break
                        if self.buffer[1] == 0:
                            self.rtp(self.buffer[4:4 + size])
                        else:
                            self.stats['rtcp_packets'] += 1
                        self.buffer = self.buffer[4 + size:]
                    elif b'\r\n\r\n' in self.buffer:
                        self.response()
                        self.stats['keepalive_replies'] += 1
                    else:
                        break
            return dict(options=options[0], setup=setup_head, play=play_head)
        finally:
            try:
                self.request('TEARDOWN')
            except (OSError, EOFError):
                pass
            self.sock.close()
            for sock in udp:
                sock.close()
            selector.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--transport', choices=['udp', 'tcp'], default='udp')
    parser.add_argument('--url', default='rtsp://192.168.0.1:554/live.ch01')
    parser.add_argument('--seconds', type=int, default=12)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = {}
    with open(args.output, 'wb') as output:
        capture = Capture(args.url, args.transport, output)
        try:
            result.update(capture.run(args.seconds))
        except Exception as error:
            result['error'] = str(error)
        result.update(stats=dict(capture.stats), nal_types=dict(capture.nals), sdp=capture.sdp)
    Path(args.output + '.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
