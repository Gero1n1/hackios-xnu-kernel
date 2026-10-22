#!/usr/bin/env python3
"""Extract selected IPSW disk-image files on NTFS and finish LZBITMAP ADS.

Regular files only are materialized. POSIX links/owners/modes stay in a UTF-8
manifest for the APFS builder, avoiding Windows symlink privileges. Existing
unowned destinations are refused; no directory is recursively deleted.
"""
import argparse
import importlib.util
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('select_system', ROOT / 'ios165/select-system.py')
select_system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(select_system)


def host_path(path):
    """Use extended Windows paths for Apple's deeply nested framework bundles."""
    path = Path(path).absolute()
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        return Path('\\\\?\\' + str(path))
    return path


def posix_parts(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or '\\' in name or any(
            p in ('.', '..') or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError(f'unsafe archive path: {name!r}')
    return parts


def safe_path(name):
    parts = posix_parts(name)
    if any(p.rstrip(' .') != p or any(c in p for c in ':<>"|?*')
            or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                         *(f'COM{i}' for i in range(1, 10)),
                                         *(f'LPT{i}' for i in range(1, 10))}
            for p in parts):
        raise ValueError(f'path cannot be safely extracted to NTFS: {name!r}')
    return parts


def parse_listing(path):
    # The upstream parser currently uses the host locale. Parse explicitly as
    # UTF-8 here, so non-ASCII Apple bundle names survive a Russian Windows host.
    entry = {}
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            line = line.rstrip('\r\n')
            if not line:
                if 'Path' in entry and 'Mode' in entry:
                    yield entry
                entry = {}
            elif ' = ' in line or line.endswith(' ='):
                key, _, value = line.partition(' =')
                entry[key] = value[1:] if value.startswith(' ') else value
    if 'Path' in entry and 'Mode' in entry:
        yield entry


def finish(destination, entries, decoder, remapped=None):
    expanded = 0
    for entry in entries:
        if not entry['Mode'].startswith('-'):
            continue
        logical = entry['Path']
        path = host_path(destination / (remapped or {}).get(logical, logical))
        header = Path(str(path) + ':com.apple.decmpfs')
        if header.exists():
            subprocess.run([str(decoder), str(path)], check=True)
            if header.exists() or Path(str(path) + ':com.apple.ResourceFork').exists():
                raise ValueError(f'LZBITMAP streams remain: {path}')
            expanded += 1
        if not path.is_file() or path.stat().st_size != int(entry['Size']):
            raise ValueError(f'missing/wrong extracted file size: {path}')
    return expanded


def remap_files(image, destination, files, remapped, sevenzip):
    expected = {e['Path']: int(e['Size']) for e in files if e['Path'] in remapped}
    remaining = {name: size for name, size in expected.items()
                 if not (destination / remapped[name]).is_file()
                 or (destination / remapped[name]).stat().st_size != size}
    if not remaining:
        return
    total = sum(remaining.values())
    if total > 64 << 20:
        raise ValueError('remapped-name batch exceeds 64 MiB bound')
    names = destination.with_name(destination.name + '.remap.files')
    names.write_text(''.join(name + '\n' for name in remaining), encoding='utf-8')
    log = destination.with_name(destination.name + '.remap.log')
    with log.open('wb') as errors:
        process = subprocess.Popen([str(sevenzip), 'x', '-so', '-spd', '-ssc', '-bb1',
                                    '-bso2', '-sccUTF-8', '-scsUTF-8', str(image), '@' + str(names)],
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            data = process.stdout.read(total + 1)
            status = process.wait(timeout=600)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    order = [line[2:].replace('\\', '/') for line in log.read_text(encoding='utf-8').splitlines()
             if line.startswith('- ')]
    if status != 0 or len(data) != total or len(order) != len(remaining) or set(order) != set(remaining):
        raise ValueError(f'remapped extraction failed/order ambiguous; see {log}')
    offset = 0
    for name in order:
        path = destination / remapped[name]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data[offset:offset + remaining[name]])
        offset += remaining[name]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('root', 'os', 'app'))
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--sevenzip', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--finish-only', action='store_true', help='reuse completed regular-file extraction')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('this adapter expects Windows NTFS alternate streams')
    image, destination = args.image.resolve(strict=True), args.output.resolve()
    owner = destination.with_name(destination.name + '.extract-source.json')
    identity = dict(image=str(image), size=image.stat().st_size, kind=args.kind)
    if destination.exists():
        if destination.is_symlink() or not owner.is_file() or json.loads(owner.read_text()) != identity:
            raise ValueError('existing destination is not owned by this extraction')
    else:
        destination.mkdir(parents=True)
        with owner.open('x') as stream:
            json.dump(identity, stream, indent=2)
    listing = destination.with_name(destination.name + '.slt')
    if not args.finish_only:
        with listing.open('wb') as stream:
            subprocess.run([str(args.sevenzip.resolve()), 'l', '-slt', '-sccUTF-8', str(image)],
                           stdout=stream, check=True)
    # Windows 7-Zip prints native separators; selection and the APFS manifest
    # use POSIX paths. Normalize only path-valued fields, not arbitrary text.
    if not args.finish_only:
        normalized = listing.with_name(listing.name + '.normalized')
        with listing.open(encoding='utf-8') as src, normalized.open('w', encoding='utf-8', newline='\n') as dst:
            for line in src:
                dst.write(line.replace('\\', '/') if line.startswith(
                    ('Path = ', 'Symbolic Link = ')) else line)
        normalized.replace(listing)
    if args.kind == 'root':
        env = dict(os.environ, PYTHONUTF8='1')
        if not args.finish_only:
            subprocess.run([sys.executable, str(ROOT / 'ios165/select-system.py'), str(listing),
                            str(destination.with_name('system.files')),
                            str(destination.with_name('system.manifest'))], env=env, check=True)
        selected = set(destination.with_name('system.files').read_text(encoding='utf-8').splitlines())
        entries = [e for e in parse_listing(listing) if e['Path'] in selected]
    elif args.kind == 'os':
        entries = [e for e in parse_listing(listing) if not e['Path'].startswith('System/DriverKit/')
                   and not e['Path'].endswith('.symbols')]
    else:
        entries = list(parse_listing(listing))
    files = [e for e in entries if e['Mode'].startswith('-')]
    seen = {}
    remapped = {}
    for entry in files:
        logical = entry['Path']
        posix_parts(logical)
        if logical.startswith('__ntfs_names/'):
            raise ValueError('reserved extraction prefix in archive')
        key = logical.casefold()
        try:
            safe_path(logical)
            conflict = key in seen and seen[key] != logical
        except ValueError:
            conflict = True
        if conflict:
            remapped[logical] = '__ntfs_names/' + hashlib.sha256(logical.encode()).hexdigest()
        seen[key] = logical
    names = destination.with_name(destination.name + '.files')
    names.write_text(''.join(e['Path'] + '\n' for e in files if e['Path'] not in remapped), encoding='utf-8')
    log = destination.with_name(destination.name + '.extract.log')
    status = None
    if not args.finish_only:
        with log.open('wb') as stream:
            completed = subprocess.run([str(args.sevenzip.resolve()), 'x', '-y', '-sns', '-spd', '-ssc', '-scsUTF-8',
                                        '-sccUTF-8', '-o' + str(destination), str(image), '@' + str(names)],
                                       stdout=stream, stderr=subprocess.STDOUT)
        status = completed.returncode
    # 7z reports unsupported ZBM as exit 2, but leaves its data in NTFS ADS.
    # The decoder must succeed, and every selected regular file must match size.
    if status not in (None, 0, 2):
        raise RuntimeError(f'7-Zip failed ({status}), see {log}')
    # APFS permits names such as "*.events.shazam.apple.com~iphone.json".
    # Preserve their bytes under safe host names, then restore the real name
    # when assembling APFS Nodes. -spd/-ssc make selection literal and exact.
    remap_files(image, destination, files, remapped, args.sevenzip.resolve())
    destination.with_name(destination.name + '.names.json').write_text(json.dumps(remapped, indent=2), encoding='utf-8')
    expanded = finish(destination, entries, args.decoder.resolve(strict=True), remapped)
    result = dict(files=len(files), expanded_lzbitmap=expanded, sevenzip_exit=status,
                  resumed_finish=args.finish_only,
                  sizes_verified=True, remapped_names=len(remapped))
    destination.with_name(destination.name + '.verified.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
