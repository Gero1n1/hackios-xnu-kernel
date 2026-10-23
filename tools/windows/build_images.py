#!/usr/bin/env python3
"""Build QEMU APFS images using upstream programs and virtual POSIX trees.

No physical symlinks, hard-link copies, Linux shell or MSVC SDK are required.
Apple files must first be extracted by extract_ios.py. No phone is accessed.
This produces the RAM-root recipe only, not an installation/flash image.
"""
import argparse
import os
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/apfs'))
import mkapfs
from extract_ios import parse_listing, posix_parts, select_system, host_path

JOBS = '''com.apple.notifyd com.apple.cfprefsd.xpc.daemon
com.apple.cfprefsd.xpc.daemon.system com.apple.logd com.apple.syslogd
com.apple.distnoted.xpc.daemon com.apple.diagnosticd com.apple.fseventsd
com.apple.containermanagerd com.apple.containermanagerd.system
com.apple.runningboardd com.apple.backboardd com.apple.mobile.usermanagerd
com.apple.mobilegestalt.xpc com.apple.lsd com.apple.powerd com.apple.analyticsd
com.apple.logd_helper com.apple.iconservices.iconservicesagent com.apple.pluginkit.pkd
com.apple.SpringBoard com.apple.wifid'''.split()


def directory(name=''):
    return mkapfs.Node(name, stat.S_IFDIR | 0o755)


def parent(root, path):
    parts = posix_parts(path)
    node = root
    for part in parts[:-1]:
        node = node.children.setdefault(part, directory(part))
        if not stat.S_ISDIR(node.mode):
            raise ValueError(f'non-directory ancestor: {path}')
    return node, parts[-1]


def put(root, path, node):
    owner, name = parent(root, path)
    node.name = name
    owner.children[name] = node


def regular(root, path, source, mode=0o644):
    if not Path(source).is_file():
        raise ValueError(f'missing input: {source}')
    put(root, path, mkapfs.Node('', stat.S_IFREG | mode, source=str(source)))


def link(root, path, target):
    put(root, path, mkapfs.Node('', stat.S_IFLNK | 0o777, target=target.encode()))


def lf_copy(source, destination):
    destination.write_bytes(Path(source).read_bytes().replace(b'\r\n', b'\n'))
    return destination


def remove(root, path):
    node = root
    parts = posix_parts(path)
    for part in parts[:-1]:
        node = node.children.get(part)
        if node is None or not stat.S_ISDIR(node.mode):
            return
    node.children.pop(parts[-1], None)


def from_manifest(folder, manifest):
    root = directory()
    names_path = folder.with_name(folder.name + '.names.json')
    names = json.loads(names_path.read_text(encoding='utf-8')) if names_path.exists() else {}
    for line in Path(manifest).read_text(encoding='utf-8').splitlines():
        mode, uid, gid, path, *target = line.split('\t')
        mode = int(mode, 8)
        posix_parts(path)
        host_name = names.get(path, path)
        posix_parts(host_name)
        source = host_path(folder / host_name)
        if stat.S_ISREG(mode) and not source.is_file() and path.startswith('Applications/'):
            staged = 'private/var/staged_system_apps/' + path[len('Applications/'):]
            source = host_path(folder / names.get(staged, staged))
        if stat.S_ISREG(mode):
            if not source.is_file():
                raise ValueError(f'missing selected file: {path}')
            node = mkapfs.Node('', mode, source=str(source))
        elif stat.S_ISDIR(mode):
            owner, name = parent(root, path)
            node = owner.children.get(name, directory())
            if not stat.S_ISDIR(node.mode):
                raise ValueError(f'conflicting manifest directory: {path}')
            node.mode = mode
        elif stat.S_ISLNK(mode):
            node = mkapfs.Node('', mode, target=target[0].encode('utf-8'))
        else:
            continue
        node.uid, node.gid = int(uid), int(gid)
        put(root, path, node)
    return root


def listing_manifest(listing, destination):
    lines = []
    for entry in parse_listing(listing):
        path = entry['Path']
        # The OS extraction omits DriverKit and the unused cache symbols.
        if path == 'System/DriverKit' or path.startswith('System/DriverKit/') or path.endswith('.symbols'):
            continue
        mode = select_system.mode_bits(entry['Mode'])
        if mode is not None:
            lines.append(f'{mode:o}\t{entry.get("User ID") or 0}\t{entry.get("Group ID") or 0}\t'
                         f'{path}\t{entry.get("Symbolic Link", "")}\n')
    destination.write_text(''.join(lines), encoding='utf-8')


def image(tree, name, output):
    spool_path = output.with_name(output.name + '.spool')
    with spool_path.open('x+b') as spool:
        volume = mkapfs.Volume(0, name, tree, True, 1683900000 * 10**9, spool, workers=1)
        container = mkapfs.Container([volume], 256, output.name)
        container.layout()
        container.write(str(output))
    spool_path.unlink()  # only our completed temporary compression spool
    print(f'{output}: {output.stat().st_size} bytes; {volume.counts}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    parser.add_argument('--small-only', action='store_true', help='rebuild root.apfs, reuse an existing system image')
    parser.add_argument('--oslog', action='append', metavar='JOB_LABEL',
                        help='send this job os_log to console; repeat for selected jobs')
    args = parser.parse_args()
    ios = ROOT / 'ios165'
    required = [ios / 'system.manifest', ios / 'rootfs.slt', ios / 'cryptex.slt',
                ios / 'cryptex/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64',
                ios / 'cryptex-app/usr/libexec/webbookmarksd']
    for path in required:
        if not path.exists():
            parser.error(f'missing input: {path}')
    output = args.output.resolve()
    if shutil.disk_usage(output.parent).free < ((128 << 20) if args.small_only else (6 << 30)):
        parser.error('need 6 GiB free for output + compression spool + reserve')
    output.mkdir(exist_ok=False)
    system = from_manifest(ios / 'rootfs', ios / 'system.manifest')
    for path in ('System/Library/LaunchDaemons', 'System/Library/NanoLaunchDaemons',
                 'System/Library/LaunchAngels', 'System/Library/xpc/launchd.plist',
                 'private/var/staged_system_apps'):
        remove(system, path)
    link(system, 'System/Library/LaunchDaemons', '/redwood/LaunchDaemons')
    for path in ('redwood', 'System/Volumes/iOS', 'private/var'):
        put(system, path, directory())
    regular(system, 'sbin/launchd.ios', ios / 'rootfs/sbin/launchd', 0o755)
    regular(system, 'sbin/launchd', ROOT / 'prebuilt/userland/stage0', 0o755)
    for kind, source in (('OS', 'cryptex'), ('App', 'cryptex-app')):
        manifest = output / (kind + '.manifest')
        listing_manifest(ios / (source + '.slt'), manifest)
        subtree = from_manifest(ios / source, manifest)
        put(system, 'System/Cryptexes/' + kind, subtree)
    link(system, 'System/Library/Audio/Plug-Ins/HAL/RedwoodAudio.driver',
         '/redwood/lib/RedwoodAudio.driver')
    link(system, 'System/Library/Extensions/RedwoodMetal.bundle', '/redwood/lib/RedwoodMetal.bundle')

    # Only small generated metadata lives on disk; system file Nodes point at
    # the original extraction, so there is no second multi-GB sysroot copy.
    small = directory()
    for path in ('dev', 'private/var', 'empty', 'xpc', 'hal', 'LaunchDaemons'):
        put(small, path, directory())
    regular(small, 'sbin/init', ROOT / 'prebuilt/userland/init', 0o755)
    for source in (ROOT / 'prebuilt/userland').iterdir():
        if source.name.startswith('lib') and source.suffix == '.dylib':
            regular(small, 'lib/' + source.name, source, 0o755)
        elif source.name not in ('init', 'stage0', 'RedwoodAudio', 'SHA256SUMS'):
            regular(small, 'bin/' + source.name, source, 0o755)
    regular(small, 'lib/RedwoodAudio.driver/RedwoodAudio', ROOT / 'prebuilt/userland/RedwoodAudio', 0o755)
    regular(small, 'lib/RedwoodAudio.driver/Info.plist', ROOT / 'userland/src/audio_plugin.plist')
    regular(small, 'etc/rc', lf_copy(ROOT / 'userland/rc.launchd', output / 'rc'))
    regular(small, 'etc/rc.jobs', lf_copy(ROOT / 'userland/rc.jobs', output / 'rc.jobs'))
    pattern = output / 'pattern.bin'
    pattern.write_bytes(bytes((i*31 + (i >> 8)) & 255 for i in range(300000)))
    regular(small, 'share/pattern.bin', pattern)
    motd = output / 'motd'
    motd.write_bytes(b'Darwin on QEMU: Windows image recipe\n')
    regular(small, 'etc/motd', motd)
    skeleton = output / 'var.skel'
    with skeleton.open('wb') as stream:
        subprocess.run([sys.executable, str(ios / 'mkvarskel.py'), str(ios / 'rootfs.slt')],
                       env=dict(os.environ, PYTHONUTF8='1'), stdout=stream, check=True)
    text = skeleton.read_text(encoding='utf-8').replace(
        'l\t/usr/share/zoneinfo\t/private/var/db/timezone/zoneinfo',
        'l\t/usr/share/zoneinfo.default\t/private/var/db/timezone/zoneinfo')
    for source in (ROOT / 'userland/var').rglob('*'):
        if source.is_file():
            rel = source.relative_to(ROOT / 'userland/var').as_posix()
            owner = 501 if rel.startswith('mobile/') else 0
            regular(small, 'var/' + rel, source)
            text += f'f\t644\t{owner}\t{owner}\t/private/var/{rel}\t/var/{rel}\n'
    text += 'd\t700\t501\t501\t/private/var/mobile/tmp\n'
    skeleton.write_bytes(text.encode('utf-8'))
    regular(small, 'etc/var.skel', skeleton)
    inject = output / 'inject.conf'
    inject.write_text('/redwood/lib/libredwood.dylib backboardd containermanagerd containermanagerd_system\n'
                      '/redwood/lib/libredwood-sb.dylib SpringBoard\n'
                      '/redwood/lib/libredwood-media.dylib mediaserverd\n'
                      '/redwood/lib/libredwood-app.dylib Sileo\n'
                      '/redwood/lib/libredwood-tweaks.dylib Preferences\n', encoding='ascii', newline='\n')
    regular(small, 'etc/inject.conf', inject)
    jobs = output / 'LaunchDaemons'
    subprocess.run([sys.executable, str(ROOT / 'userland/mkjobs.py'), '--rest',
                    str(ios / 'rootfs'), str(jobs), *JOBS],
                   env=dict(os.environ, OSLOG=' '.join(args.oslog or ['com.apple.backboardd'])), check=True)
    for source in jobs.iterdir():
        regular(small, 'LaunchDaemons/' + source.name, source)
    for name in ('demo', 'wifi', 'varstore'):
        source = ROOT / f'userland/com.redwood.{name}.plist'
        regular(small, 'LaunchDaemons/' + source.name, source)
    image(small, 'redwood', output / 'root.apfs')
    if not args.small_only:
        image(system, 'System', output / 'system.apfs')


if __name__ == '__main__':
    main()
