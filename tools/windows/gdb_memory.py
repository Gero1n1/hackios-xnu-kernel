"""Bounded RAM preload into a paused local QEMU; no physical handset access.

Uses QEMU's documented physical-memory GDB mode. Streaming avoids the raw
loader's multi-GiB ROM copy on Windows. Every packet is acknowledged and
checksummed; sample readbacks and the streamed input hash are recorded.
"""
import hashlib
import socket
import time


def escape(data):
    return data.replace(b'}', b'}]').replace(b'#', b'}\x03').replace(b'$', b'}\x04').replace(b'*', b'}\x0a')


def unescape(data):
    out = bytearray()
    i = 0
    while i < len(data):
        ch = data[i]
        i += 1
        if ch == 125:
            if i == len(data):
                raise ValueError('truncated RSP escape')
            out.append(data[i] ^ 32)
            i += 1
        elif ch == 42:
            if not out or i == len(data) or not 3 <= data[i] - 29 <= 97:
                raise ValueError('invalid RSP run length')
            out.extend(out[-1:] * (data[i] - 29))
            i += 1
        else:
            out.append(ch)
        if len(out) > 1 << 20:
            raise ValueError('oversized decoded RSP response')
    return bytes(out)


class GDBMemory:
    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buffer = b''
        self.pos = 0
        self.no_ack = False
        try:
            features = self.call(b'qSupported').decode('ascii').split(';')
            sizes = [int(f.split('=')[1], 16) for f in features if f.startswith('PacketSize=')]
            self.packet_size = sizes[0] if sizes else 4096
            if not 256 <= self.packet_size <= 1 << 20:
                raise ValueError('invalid GDB packet-size bound')
            if b'OK' == self.call(b'QStartNoAckMode'):
                self.no_ack = True
            if self.call(b'Qqemu.PhyMemMode:1') != b'OK' or self.call(b'qqemu.PhyMemMode') != b'1':
                raise RuntimeError('QEMU physical-memory mode unavailable')
        except BaseException:
            self.close()
            raise

    def byte(self):
        if self.pos == len(self.buffer):
            self.buffer = self.sock.recv(65536)
            self.pos = 0
            if not self.buffer:
                raise RuntimeError('GDB connection closed')
        value = self.buffer[self.pos]
        self.pos += 1
        return value

    def receive(self):
        for _ in range(128):
            if self.byte() == 36:
                break
        else:
            raise RuntimeError('RSP response has no packet start')
        raw = bytearray()
        while True:
            ch = self.byte()
            if ch == 35:
                break
            raw.append(ch)
            if ch in (125, 42):
                # A run-length count can itself be '}', without introducing
                # an escape. Consume it here before looking for framing.
                raw.append(self.byte())
            if len(raw) > 1 << 20:
                raise RuntimeError('oversized RSP packet')
        checksum = int(bytes((self.byte(), self.byte())), 16)
        if sum(raw) & 255 != checksum:
            raise RuntimeError('RSP checksum mismatch')
        if not self.no_ack:
            self.sock.sendall(b'+')
        return unescape(raw)

    def call(self, payload):
        wire = escape(payload)
        frame = b'$' + wire + b'#' + f'{sum(wire) & 255:02x}'.encode()
        for _ in range(3):
            self.sock.sendall(frame)
            if self.no_ack:
                break
            ack = self.byte()
            if ack == 43:
                break
            if ack != 45:
                raise RuntimeError('invalid RSP acknowledgment')
        else:
            raise RuntimeError('RSP request repeatedly rejected')
        reply = self.receive()
        if reply.startswith(b'E'):
            raise RuntimeError(f'GDB target error: {reply!r}')
        return reply

    def read(self, address, size):
        if not 0 < size <= self.packet_size // 2:
            raise ValueError('read size outside packet bound')
        data = bytes.fromhex(self.call(f'm{address:x},{size:x}'.encode()).decode('ascii'))
        if len(data) != size:
            raise RuntimeError('short GDB memory read')
        return data

    def load(self, path, address, deadline, progress=None):
        size = path.stat().st_size
        if not size or address < 0 or address + size > 1 << 64:
            raise ValueError('invalid RAM preload span')
        digest = hashlib.sha256()
        done = 0
        samples = []
        next_progress = 0
        with path.open('rb') as stream:
            while data := stream.read((self.packet_size - 64) // 2):
                if time.monotonic() >= deadline:
                    raise TimeoutError('RAM preload startup deadline exceeded')
                # QEMU's stub supports hex M writes; this Windows build returns
                # an empty unsupported response for binary X writes.
                reply = self.call(f'M{address + done:x},{len(data):x}:'.encode() + data.hex().encode())
                if reply != b'OK':
                    raise RuntimeError('GDB memory write was not accepted')
                if not samples or done >= size // 2 and len(samples) == 1:
                    samples.append((done, data[:64]))
                digest.update(data)
                done += len(data)
                if progress and done >= next_progress:
                    progress(done, size)
                    next_progress = done + (128 << 20)
            stream.seek(max(0, size - 64))
            samples.append((max(0, size - 64), stream.read(64)))
        if done != size:
            raise RuntimeError('input changed size during RAM preload')
        for offset, expected in samples:
            if self.read(address + offset, len(expected)) != expected:
                raise RuntimeError(f'RAM preload readback failed at {offset:#x}')
        return dict(bytes=done, sha256=digest.hexdigest(), readback_samples=len(samples),
                    packet_size=self.packet_size, no_ack=self.no_ack)

    def detach(self):
        if self.call(b'Qqemu.PhyMemMode:0') != b'OK' or self.call(b'D') != b'OK':
            raise RuntimeError('GDB detach failed')

    def close(self):
        self.sock.close()
