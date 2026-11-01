from collections import Counter
from io import BytesIO
import struct
import unittest
import socket
import threading
import time
import itertools

from capture_rtsp import Capture
from legacy_receiver import GuardedCapture, KeyframeGate


class RtpTests(unittest.TestCase):
    def capture(self):
        c = Capture.__new__(Capture)
        c.output = BytesIO()
        c.stats = Counter()
        c.nals = Counter()
        c.fragment = None
        c.last_sequence = None
        c.last_fragment_sequence = None
        return c

    def packet(self, seq, payload):
        return struct.pack('!BBHII', 128, 96, seq, 0, 1234) + payload

    def test_fragment_reassembly_and_missing_packet(self):
        c = self.capture()
        c.rtp(self.packet(1, b'\x7c\x85first'))
        c.rtp(self.packet(2, b'\x7c\x45last'))
        self.assertEqual(c.output.getvalue(), b'\x00\x00\x00\x01\x65firstlast')
        self.assertEqual(c.nals[5], 1)
        c.rtp(self.packet(3, b'\x7c\x85bad'))
        c.rtp(self.packet(5, b'\x7c\x45gap'))
        self.assertEqual(c.nals[5], 1)
        self.assertEqual(c.stats['broken_fragments'], 1)

    def test_stap_packet_extracts_both_parameter_sets(self):
        c = self.capture()
        c.rtp(self.packet(1, b'\x78\x00\x03\x67aa\x00\x02\x68b'))
        self.assertEqual(c.nals, Counter({7: 1, 8: 1}))
        self.assertEqual(c.output.getvalue(), b'\x00\x00\x00\x01\x67aa\x00\x00\x00\x01\x68b')

    def test_packet_loss_suppresses_dependent_frames_until_complete_keyframe(self):
        sent = []
        gate = KeyframeGate(sent.append)
        c = self.capture()
        c.__class__ = GuardedCapture
        c.output = gate
        c.rtp(self.packet(1, b'\x67sps'))
        c.rtp(self.packet(2, b'\x68pps'))
        c.rtp(self.packet(3, b'\x41before-idr'))
        self.assertEqual(gate.dropped, 1)
        c.rtp(self.packet(4, b'\x65idr'))
        c.rtp(self.packet(5, b'\x41good'))
        count = len(sent)
        c.rtp(self.packet(7, b'\x41missing-reference'))
        c.rtp(self.packet(8, b'\x7c\x85broken-idr-start'))
        c.rtp(self.packet(10, b'\x7c\x45broken-idr-end'))
        c.rtp(self.packet(11, b'\x41still-missing-reference'))
        self.assertEqual(len(sent), count)
        c.rtp(self.packet(12, b'\x7c\x85new-'))
        c.rtp(self.packet(13, b'\x7c\x45idr'))
        c.rtp(self.packet(14, b'\x41recovered'))
        self.assertEqual(sent[-4:], [b'\0\0\0\1\x67sps', b'\0\0\0\1\x68pps',
                                     b'\0\0\0\1\x65new-idr', b'\0\0\0\1\x41recovered'])
        self.assertFalse(gate.waiting)

    def test_new_ssrc_requires_a_new_keyframe(self):
        sent = []
        c = self.capture()
        c.__class__ = GuardedCapture
        c.output = KeyframeGate(sent.append)
        c.rtp(self.packet(1, b'\x65first'))
        changed = struct.pack('!BBHII', 128, 96, 2, 0, 9876) + b'\x41different-stream'
        c.rtp(changed)
        self.assertEqual(sent, [b'\0\0\0\1\x65first'])


class InterleavedTcpTests(unittest.TestCase):
    def test_partial_tcp_reads_and_keepalive_do_not_repeat_or_omit_nals(self):
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(1)
        port=listener.getsockname()[1]
        url=f'rtsp://127.0.0.1:{port}/live.ch01'
        errors=[]
        methods=[]
        nals=[b'\x41first\r\n\r\n$payload',b'\x65'+bytes(range(256))*40,b'\x41after-keepalive',b'\x41last']
        sequence=65530
        def packet(payload,channel=0):
            nonlocal sequence
            rtp=struct.pack('!BBHII',128,96,sequence,0,1234)+payload
            sequence=(sequence+1)&65535
            return b'$'+bytes([channel])+struct.pack('!H',len(rtp))+rtp
        first=packet(nals[0])
        body=nals[1][1:]
        chunks=[body[i:i+1000] for i in range(0,len(body),1000)]
        for i,chunk in enumerate(chunks):
            first+=packet(bytes([0x7c,5|(128 if i==0 else 0)|(64 if i==len(chunks)-1 else 0)])+chunk)
        # RTCP has its own sequence space; do not advance the RTP sequence.
        rtcp=b'\x80\xc8\x00\x01'+b'\0'*4
        first+=b'$\x01'+struct.pack('!H',len(rtcp))+rtcp
        after=packet(nals[2])+packet(nals[3])
        def serve():
            try:
                connection,_=listener.accept()
                with connection:
                    connection.settimeout(7)
                    connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    buffer=b''
                    while True:
                        while b'\r\n\r\n' not in buffer:
                            data=connection.recv(4096)
                            if not data:return
                            buffer+=data
                        request,buffer=buffer.split(b'\r\n\r\n',1)
                        lines=request.decode().split('\r\n')
                        method=lines[0].split()[0]
                        methods.append(method)
                        if method=='TEARDOWN':return
                        cseq=next(line.split(':',1)[1].strip() for line in lines if line.lower().startswith('cseq:'))
                        body=b'a=control:track0\r\n' if method=='DESCRIBE' else b''
                        response=(f'RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: test\r\nContent-Base: {url}/\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
                        if method=='PLAY':response+=first
                        elif method=='OPTIONS' and 'PLAY' in methods:response+=after
                        offset=0
                        for size in itertools.cycle((1,3,2,7,1024,11,4096)):
                            if offset>=len(response):break
                            connection.sendall(response[offset:offset+size])
                            offset+=size
                            time.sleep(.001)
            except Exception as exc:errors.append(repr(exc))
        thread=threading.Thread(target=serve,daemon=True)
        thread.start()
        try:
            output=BytesIO()
            capture=Capture(url,'tcp',output)
            capture.run(4.5)
            self.assertEqual(output.getvalue(),b''.join(b'\0\0\0\1'+nal for nal in nals))
            self.assertEqual(capture.stats['sequence_gaps'],0)
            self.assertEqual(capture.stats['rtcp_packets'],1)
            self.assertEqual(capture.stats['keepalive_replies'],1)
        finally:
            thread.join(timeout=2)
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])


if __name__ == '__main__':
    unittest.main()
