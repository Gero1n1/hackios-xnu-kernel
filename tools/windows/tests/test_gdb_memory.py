import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gdb_memory import GDBMemory, escape, unescape


class Memory(GDBMemory):
    def __init__(self, size=257):
        self.packet_size = size
        self.no_ack = False
        self.memory = bytearray(50000)
        self.writes = 0

    def call(self, payload):
        self.assert_packet = len(escape(payload)) <= self.packet_size
        if not self.assert_packet:
            raise AssertionError('packet limit exceeded')
        command, data = payload.split(b':', 1)
        if command[:1] != b'M':
            raise AssertionError('expected supported hex memory write')
        data = bytes.fromhex(data.decode())
        address, size = [int(v, 16) for v in command[1:].split(b',')]
        if size != len(data):
            raise AssertionError('size disagreement')
        self.memory[address:address+size] = data
        self.writes += 1
        return b'OK'

    def read(self, address, size):
        return bytes(self.memory[address:address+size])


class GDBMemoryTests(unittest.TestCase):
    def transport(self, data):
        class Socket:
            def __init__(self):
                self.received = iter(data)
                self.sent = []
            def recv(self, size):
                try:
                    return bytes([next(self.received)])
                except StopIteration:
                    return b''
            def sendall(self, payload):
                self.sent.append(payload)
        target = GDBMemory.__new__(GDBMemory)
        target.sock = Socket()
        target.buffer = b''
        target.pos = 0
        target.no_ack = False
        return target

    def test_fragmented_transport_and_request_retry(self):
        target = self.transport(b'-+$OK#9a')
        self.assertEqual(target.call(b'fixture'), b'OK')
        self.assertEqual(target.sock.sent[0], target.sock.sent[1])
        self.assertEqual(target.sock.sent[-1], b'+')

    def test_checksum_and_target_errors_are_failures(self):
        for data, message in [(b'+$OK#00', 'checksum'), (b'+$E01#a6', 'target error')]:
            with self.assertRaisesRegex(RuntimeError, message):
                self.transport(data).call(b'fixture')

    def test_truncated_transport_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.transport(b'+$O').call(b'fixture')

    def test_run_length_count_can_be_escape_character(self):
        self.assertEqual(self.transport(b'+$0*}#d7').call(b'fixture'), b'0' * 97)

    def test_all_bytes_escape_roundtrip(self):
        data = bytes(range(256))
        self.assertEqual(unescape(escape(data)), data)

    def test_run_length_and_malformed_encoding(self):
        self.assertEqual(unescape(b'0* '), b'0000')
        for value in (b'}', b'* ', b'0*', b'0*\x01'):
            with self.assertRaises(ValueError):
                unescape(value)

    def test_odd_packet_boundary_preserves_all_escaped_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image'
            data = (b'$#}*' * 2003) + bytes(range(256))
            path.write_bytes(data)
            memory = Memory()
            result = memory.load(path, 123, time.monotonic()+10)
            self.assertEqual(memory.memory[123:123+len(data)], data)
            self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(result['bytes'], len(data))
            self.assertEqual(result['readback_samples'], 3)
            self.assertGreater(memory.writes, 2)

    def test_deadline_before_first_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image'
            path.write_bytes(b'fixture')
            memory = Memory()
            with self.assertRaises(TimeoutError):
                memory.load(path, 0, time.monotonic()-1)
            self.assertEqual(memory.writes, 0)

    def test_bad_readback_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image'
            path.write_bytes(b'fixture')
            memory = Memory()
            memory.read = lambda address, size: b'x' * size
            with self.assertRaisesRegex(RuntimeError, 'readback'):
                memory.load(path, 0, time.monotonic()+10)

    def test_empty_and_overflow_spans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image'
            path.write_bytes(b'')
            with self.assertRaises(ValueError):
                Memory().load(path, 0, time.monotonic()+10)
            path.write_bytes(b'fixture')
            with self.assertRaises(ValueError):
                Memory().load(path, (1 << 64)-1, time.monotonic()+10)


if __name__ == '__main__':
    unittest.main()
