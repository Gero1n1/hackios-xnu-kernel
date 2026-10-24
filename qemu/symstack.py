#!/usr/bin/env python3
"""Symbolicate guest user stacks using the exact extracted iOS shared cache.

usage: symstack.py LOG [PROCESS] [--cache FILE] [--ipsw PROGRAM]
Only named frames are observations; symbolication does not establish a cause.
The loader's replay after its first return is skipped. Failed lookups are never
cached as successful results, and cached names are bound to SHA-256 of both the
main shared-cache file and its matching .symbols file.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1] / 'ios165'
CACHE_LOW, CACHE_HIGH = 0x180000000, 0x280000000
HEX = re.compile(r'0x[0-9a-f]+', re.I)
HEADER = re.compile(r'(ustacks|usertrap): (\S+)\[(\d+)\].*shared cache slide (0x[0-9a-f]+)', re.I)
ABORT = re.compile(r'userabort: (\S+)\[(\d+)\] aborts, shared cache slide (0x[0-9a-f]+); called from: (.*)', re.I)


def records(lines, only=None):
    """Yield text and (address, slide) records; never invent a missing slide."""
    slide, show, thrown = None, False, None
    for line in lines:
        if 'kernel returned: log at ' in line:
            break
        if 'Darwin Kernel Version ' in line:
            slide, show, thrown = None, False, None
        line = line.rstrip('\r\n').replace('\0', '')
        if re.fullmatch(r'\((0x[0-9a-f]+ ?)+\)', line, re.I):
            thrown = HEX.findall(line)
            continue
        match = ABORT.search(line)
        if match:
            if only is None or match[1] == only:
                yield f'userabort: {match[1]}[{match[2]}]'
                for value in HEX.findall(match[4]):
                    yield int(value, 16), int(match[3], 16)
                if thrown:
                    yield '  the exception was thrown from'
                    for value in thrown:
                        yield int(value, 16), int(match[3], 16)
            thrown = None
            continue
        match = HEADER.search(line)
        if match:
            slide = int(match[4], 16)
            show = only is None or match[2] == only
            if show:
                yield line[match.start():]
            continue
        if not show or slide is None:
            continue
        match = re.search(r'ustacks:  thread (0x[0-9a-f]+)([^:]*): (.*)', line, re.I)
        if match:
            yield f'  thread {match[1]}{match[2]}'
            for depth, value in enumerate(HEX.findall(match[3])):
                if depth != 1:  # LR is usually stale; the FP chain carries the caller.
                    yield int(value, 16), slide
            continue
        match = re.search(r'usertrap:   called from (0x[0-9a-f]+)', line, re.I)
        if match:
            yield int(match[1], 16), slide
        elif 'usertrap:   x' in line:
            yield '  ' + line[line.index('usertrap:') + 10:]


def cache_identity(cache):
    identity = {}
    for name, path in [('cache', cache), ('symbols', Path(str(cache) + '.symbols'))]:
        with path.open('rb') as stream:
            identity[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return identity


def load_known(path, identity):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('identity') != identity or not isinstance(value.get('symbols'), dict):
        raise ValueError(f'{path}: symbol cache has different/unknown input identity; use a new --known file')
    known = value['symbols']
    if any(not re.fullmatch(r'0x[0-9a-f]+', k) or not isinstance(v, str) or v == '?'
           for k, v in known.items()):
        raise ValueError(f'{path}: invalid cached symbols')
    return known


class Resolver:
    def __init__(self, ipsw, cache, known_path, identity, max_lookups=256):
        self.ipsw, self.cache = str(ipsw), str(cache)
        self.known_path, self.identity = known_path, identity
        self.known = load_known(known_path, identity)
        self.max_lookups, self.lookups = max_lookups, 0

    def name(self, address, slide):
        unslid = address - slide
        if not CACHE_LOW <= unslid < CACHE_HIGH:
            return f'{address:#x} (not in the shared cache)'
        key = f'{unslid:#x}'
        if key not in self.known:
            if self.lookups >= self.max_lookups:
                raise RuntimeError('lookup bound reached; rerun to continue from verified cached names')
            self.lookups += 1
            result = subprocess.run([self.ipsw, '--no-color', 'dyld', 'a2s', self.cache, key],
                                    capture_output=True, timeout=45, check=True)
            text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            matches = re.findall(r'^' + re.escape(key) + r':\s*(.+)$', text, re.M)
            if not matches or matches[-1].strip() in ('', '?'):
                raise RuntimeError(f'ipsw returned no symbol for {key}: {text[:400]}')
            self.known[key] = matches[-1].strip()
            self.known_path.write_text(json.dumps(dict(identity=self.identity, symbols=self.known),
                                                  indent=2), encoding='utf-8')
        return f'{key}: {self.known[key]}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('process', nargs='?')
    parser.add_argument('--ipsw', default=shutil.which('ipsw'))
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--known', type=Path)
    parser.add_argument('--max-lookups', type=int, default=256)
    args = parser.parse_args()
    if not args.ipsw:
        parser.error('ipsw not on PATH; specify --ipsw PROGRAM')
    if not 1 <= args.max_lookups <= 4096:
        parser.error('max-lookups must be 1..4096')
    cache = args.cache
    if cache is None:
        choices = [p for p in (BASE / '20F66__iPhone10,3_6/dyld_shared_cache_arm64',
                              BASE / 'cryptex/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64')
                   if p.is_file()]
        if len(choices) != 1:
            parser.error('specify --cache FILE (no unique extracted shared cache)')
        cache = choices[0]
    identity = cache_identity(cache)
    known_path = args.known or BASE / ('a2s-' + identity['cache'][:12] + '-' + identity['symbols'][:12] + '.json')
    resolver = Resolver(args.ipsw, cache, known_path, identity, args.max_lookups)
    print(f'# shared-cache SHA-256 {identity["cache"]}; symbols SHA-256 {identity["symbols"]}')
    with args.log.open(encoding='utf-8', errors='replace') as stream:
        for item in records(stream, args.process):
            print('    ' + resolver.name(*item) if isinstance(item, tuple) else item, flush=True)


if __name__ == '__main__':
    main()
