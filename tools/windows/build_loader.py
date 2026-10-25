#!/usr/bin/env python3
"""Build the freestanding ARM64 EFI loader on Windows using Zig/LLVM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=ROOT / 'xnuloader/loader.c')
    parser.add_argument('--output', type=Path, default=ROOT / 'xnuloader/loader.efi')
    args = parser.parse_args()
    zig = args.zig.resolve(strict=True)
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault('ZIG_GLOBAL_CACHE_DIR', str(ROOT / 'host-tools/zig-cache'))
    # UEFI is freestanding. Avoid Zig probing for an installed MSVC libc.
    # Use LLD directly: Zig build-exe's UEFI target forced ImageBase=0 in the
    # installed version despite --image-base. The loader must prefer the same
    # low address as the Linux recipe, away from the resident system image.
    compile_cmd = [str(zig), 'cc', '-target', 'aarch64-uefi-msvc', '-nostdlib',
                   '-nostdinc', '-isystem', str(zig.parent / 'lib/include'),
                   '-I', str(ROOT / 'xnuloader'), '-Xclang', '-target-feature',
                   '-Xclang', '+el3', '-ffreestanding',
                   '-fno-builtin', '-fno-stack-protector', '-fshort-wchar',
                   '-mgeneral-regs-only', '-O1', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter', '-c', str(source), '-o', str(output) + '.obj']
    link_cmd = [str(zig), 'lld-link', '/subsystem:efi_application', '/entry:efi_main',
                '/nodefaultlib', '/base:0xA1000000', '/out:' + str(output), str(output) + '.obj']
    subprocess.run(compile_cmd, env=env, check=True)
    subprocess.run(link_cmd, env=env, check=True)
    binary = output.read_bytes()
    pe = struct.unpack_from('<I', binary, 0x3c)[0]
    if (binary[pe:pe+4] != b'PE\0\0' or struct.unpack_from('<H', binary, pe+4)[0] != 0xaa64
            or struct.unpack_from('<Q', binary, pe+24+24)[0] != 0xa1000000):
        raise ValueError('linked EFI is not ARM64 at the required preferred base')
    record = dict(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  loader_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                  commands=[compile_cmd, link_cmd], size=output.stat().st_size)
    Path(str(output) + '.build.json').write_text(json.dumps(record, indent=2))
    print(json.dumps({k: record[k] for k in ('size', 'loader_sha256')}))


if __name__ == '__main__':
    main()
