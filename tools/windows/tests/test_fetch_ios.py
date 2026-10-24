import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch_ios import RangeFile, fetch_member


class Reply(io.BytesIO):
    def __init__(self, data, start, end, total, status=206):
        super().__init__(data)
        self.status = status
        self.headers = {'Content-Range': f'bytes {start}-{end}/{total}',
                        'Content-Length': str(end - start + 1)}


class FetchTests(unittest.TestCase):
    def archive(self, method=0, payload=b'hello' * 1000):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', compression=method) as archive:
            archive.writestr('test.dmg', payload)
        data = data.getvalue()

        def opener(request, timeout):
            start, end = map(int, request.get_header('Range')[6:].split('-'))
            return Reply(data[start:end+1], start, end, len(data))

        remote = RangeFile('https://test.invalid/image', len(data), opener)
        with zipfile.ZipFile(remote) as archive:
            info = archive.getinfo('test.dmg')
        return remote, info, payload

    def test_stored_and_deflated(self):
        for method in (0, 8):
            remote, info, payload = self.archive(method)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'test.dmg'
                fetch_member(remote, info, path)
                self.assertEqual(path.read_bytes(), payload)
                fetch_member(remote, info, path)
                path.write_bytes(b'wrong')
                with self.assertRaises(ValueError):
                    fetch_member(remote, info, path)
                self.assertEqual(path.read_bytes(), b'wrong')

    def test_resume_both_methods(self):
        for method in (0, 8):
            remote, info, payload = self.archive(method)
            original = remote.opener
            failed = False

            def fail_on_data(req, timeout):
                nonlocal failed
                start = int(req.get_header('Range')[6:].split('-')[0])
                if start == 38:  # local header (30) + name (8)
                    failed = True
                    raise OSError('simulated interruption')
                return original(req, timeout)

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'test.dmg'
                remote.opener = fail_on_data
                with self.assertRaises(OSError):
                    fetch_member(remote, info, path)
                self.assertTrue(failed)
                path.with_name('test.dmg.part').write_bytes(payload[:127])
                remote.opener = original
                fetch_member(remote, info, path)
                self.assertEqual(path.read_bytes(), payload)

    def test_partial_identity_and_deflate_replay(self):
        remote, info, payload = self.archive(8)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.dmg'
            part = path.with_name('test.dmg.part')
            part.write_bytes(b'wrong prefix')
            with self.assertRaisesRegex(ValueError, 'metadata'):
                fetch_member(remote, info, path)
            meta = dict(url=remote.url, archive_size=remote.size, member=info.filename,
                        offset=info.header_offset, size=info.file_size, crc32=info.CRC,
                        method=info.compress_type, compressed_size=info.compress_size)
            path.with_name('test.dmg.part.json').write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, 'replay'):
                fetch_member(remote, info, path)
            self.assertEqual(part.read_bytes(), b'wrong prefix')

    def test_range_failures_do_not_publish(self):
        for error in ('status', 'range', 'length', 'short', 'encoding'):
            def opener(req, timeout):
                reply = Reply(b'abc', 0, 2, 3)
                if error == 'status': reply.status = 200
                if error == 'range': reply.headers['Content-Range'] = 'bytes 1-3/4'
                if error == 'length': reply.headers['Content-Length'] = '500'
                if error == 'short': reply = Reply(b'ab', 0, 2, 3)
                if error == 'encoding': reply.headers['Content-Encoding'] = 'gzip'
                return reply
            with self.assertRaises(OSError, msg=error):
                RangeFile('https://test.invalid', 3, opener).read(3)

    def test_seek_and_bounds(self):
        remote, _, _ = self.archive()
        with self.assertRaises(ValueError): remote.seek(-1)
        with self.assertRaises(ValueError): remote.seek(0, 4)
        self.assertEqual(remote.seek(0, 2), remote.size)
        self.assertEqual(remote.read(4), b'')

    def test_crc_corruption_not_published(self):
        remote, info, _ = self.archive()
        info.CRC ^= 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.dmg'
            with self.assertRaisesRegex(ValueError, 'CRC'):
                fetch_member(remote, info, path)
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name('test.dmg.part').exists())


if __name__ == '__main__':
    unittest.main()
