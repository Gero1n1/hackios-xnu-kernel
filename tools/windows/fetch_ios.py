#!/usr/bin/env python3
"""Fetch individual iOS 16.5 IPSW members with bounded, checked HTTP ranges.

No full IPSW, shell, phone connection or credentials are needed. Stored members
resume only with matching metadata; CRC and size are checked before publication.
Apple files belong in ignored ios165/, never in Git.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import shutil
import struct
import urllib.request
import zipfile
import zlib

URL = ('https://updates.cdn-apple.com/2023SpringFCS/fullrestores/032-85130/'
       '6AB0D10A-8AF9-4F6C-8CF5-A1B315CABE88/'
       'iPhone10,3,iPhone10,6_16.5_20F66_Restore.ipsw')
SIZE = 6165698198
CHUNK = 8 << 20
MEMBERS = {'app': '096-03536-083.dmg', 'root': '096-04088-077.dmg',
           'os': '096-04666-078.dmg', 'manifest': 'BuildManifest.plist'}


class RangeFile(io.RawIOBase):
    def __init__(self, url=URL, size=SIZE, opener=urllib.request.urlopen):
        self.url, self.size, self.opener, self.pos = url, size, opener, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        pos = offset + (0 if whence == 0 else self.pos if whence == 1
                        else self.size if whence == 2 else -self.size * 2)
        if whence not in (0, 1, 2) or pos < 0:
            raise ValueError('invalid seek')
        self.pos = pos
        return pos

    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else min(n, max(0, self.size - self.pos))
        if not n:
            return b''
        if n > CHUNK:
            raise ValueError('metadata/range read exceeds bounded chunk')
        start, end = self.pos, self.pos + n - 1
        req = urllib.request.Request(self.url, headers={
            'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'})
        with self.opener(req, timeout=45) as response:
            expected = f'bytes {start}-{end}/{self.size}'
            if response.status != 206 or response.headers.get('Content-Range') != expected:
                raise OSError('server did not honor exact HTTP range')
            if response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise OSError('encoded range response')
            if int(response.headers.get('Content-Length', -1)) != n:
                raise OSError('wrong range Content-Length')
            data = response.read(n + 1)
            if len(data) != n:
                raise OSError('truncated/oversized range response')
        self.pos += n
        return data


def checksum(path):
    crc = 0
    with path.open('rb') as stream:
        while data := stream.read(CHUNK):
            crc = zlib.crc32(data, crc)
    return crc


def chunks(remote, start, size, workers):
    """At most workers * CHUNK bytes buffered; emit ranges in archive order."""
    def read(offset, length):
        for attempt in range(3):
            try:
                source = RangeFile(remote.url, remote.size, remote.opener)
                source.seek(start + offset)
                return source.read(length)
            except OSError:
                if attempt == 2:
                    raise

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        cursor = 0
        while cursor < size or pending:
            while cursor < size and len(pending) < workers:
                n = min(CHUNK, size - cursor)
                pending.append(pool.submit(read, cursor, n))
                cursor += n
            yield pending.popleft().result()


def fetch_member(remote, info, output, workers=4):
    """Download a member, preserving failed partials for diagnosis.

    Deflated partials are verified against a replay from compressed byte zero;
    only the missing suffix is appended. No inflate state is guessed.
    """
    output = Path(output)
    if not 1 <= workers <= 8:
        raise ValueError('workers must be between 1 and 8')
    if info.compress_type not in (0, 8) or info.flag_bits & 1:
        raise ValueError('only unencrypted stored/deflated members are supported')
    if (info.compress_type == 0 and info.file_size != info.compress_size) or info.is_dir():
        raise ValueError('inconsistent stored member')
    if output.exists():
        if output.stat().st_size != info.file_size or checksum(output) != info.CRC:
            raise ValueError('existing output failed verification; preserved')
        print(f'{output.name}: existing file, size and CRC verified', flush=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(output.name + '.part')
    meta = output.with_name(output.name + '.part.json')
    identity = dict(url=remote.url, archive_size=remote.size, member=info.filename,
                    offset=info.header_offset, size=info.file_size, crc32=info.CRC,
                    method=info.compress_type, compressed_size=info.compress_size)
    if part.exists() or meta.exists():
        if not part.exists() or not meta.exists() or json.loads(meta.read_text()) != identity:
            raise ValueError('partial metadata missing/mismatched; files preserved')
    else:
        with meta.open('x', encoding='utf-8') as stream:
            json.dump(identity, stream, indent=2)
        part.touch(exist_ok=False)
    done = part.stat().st_size
    if done > info.file_size:
        raise ValueError('oversized partial; preserved')
    # Local and central headers must describe the same member.
    remote.seek(info.header_offset)
    header = remote.read(30)
    sig, _, flags, method = struct.unpack_from('<IHHH', header)
    if sig != 0x04034B50 or flags != info.flag_bits or method != info.compress_type:
        raise ValueError('local header disagrees with central directory')
    nl, el = struct.unpack_from('<HH', header, 26)
    name = remote.read(nl).decode('utf-8' if flags & 2048 else 'cp437')
    if name != info.filename:
        raise ValueError('local filename mismatch')
    data_start = info.header_offset + 30 + nl + el
    if data_start + info.compress_size > remote.size:
        raise ValueError('member is outside archive')
    remaining = info.file_size - done
    if shutil.disk_usage(output.parent).free < remaining + (256 << 20):
        raise OSError('not enough space for member plus 256 MiB reserve')
    inflater = zlib.decompressobj(-15) if info.compress_type == 8 else None
    cursor = 0 if inflater else done
    written = 0 if inflater else done
    with part.open('r+b') as stream:
        stream.seek(written)

        def emit(data):
            nonlocal written
            if written + len(data) > info.file_size:
                raise ValueError('inflate exceeded member size')
            overlap = min(len(data), max(0, done - written))
            if overlap and stream.read(overlap) != data[:overlap]:
                raise ValueError('partial differs from replay; preserved')
            stream.write(data[overlap:])
            written += len(data)

        # Each response is validated before any of it is appended.
        for data in chunks(remote, data_start + cursor, info.compress_size - cursor, workers):
            if inflater:
                while data:
                    emit(inflater.decompress(data, CHUNK))
                    data = inflater.unconsumed_tail
                if inflater.unused_data:
                    raise ValueError('trailing bytes after deflate stream')
            else:
                emit(data)
            print(f'{output.name}: {written}/{info.file_size}', flush=True)
        if inflater and not inflater.eof:
            raise ValueError('incomplete deflate stream')
    if part.stat().st_size != info.file_size or checksum(part) != info.CRC:
        raise ValueError('member size/CRC mismatch; partial preserved')
    # Never replace an existing final file.
    part.rename(output)
    meta.unlink()
    print(f'{output.name}: size and CRC verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('member', choices=MEMBERS)
    parser.add_argument('--output-dir', type=Path, default=Path('ios165/downloads'))
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    remote = RangeFile()
    with zipfile.ZipFile(remote) as archive:
        info = archive.getinfo(MEMBERS[args.member])
    fetch_member(remote, info, args.output_dir / info.filename, args.workers)


if __name__ == '__main__':
    main()
