#!/usr/bin/env python3
"""Capture a local QEMU ticket-lock timeout before XNU's panic debugger.

Attach only after run_qemu.py has finished RAM streaming. This does not start
a VM, touch a handset, write guest memory, change timeouts, or certify a boot.
The output describes a diagnostic capture, never a passing system test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import time
import xml.etree.ElementTree as ET

from gdb_memory import escape, unescape  # Pure RSP framing only; no RAM API.

LIMIT = 1 << 20
U64 = 1 << 64
CORE_NAMES = [f'x{i}' for i in range(31)] + ['sp', 'pc', 'cpsr']


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def bounded_json(path):
    with Path(path).open('rb') as stream:
        data = stream.read(LIMIT+1)
    if len(data) > LIMIT:
        raise ValueError('oversized JSON metadata')
    return json.loads(data)


def prepare(run, artifact):
    """Validate local input provenance before connecting to any endpoint."""
    run, artifact = Path(run), Path(artifact)
    command = bounded_json(run / 'command.json')
    progress = bounded_json(run / 'progress.json')
    if (run / 'result.json').exists():
        raise ValueError('run has already finished; refusing stale endpoint')
    if progress.get('status') != 'running' or not progress.get('kernel_started_host_seconds'):
        raise ValueError('run is not past RAM streaming and kernel startup')
    transfer = progress.get('system_transfer', {})
    system = command.get('inputs', {}).get('system', {})
    if not system or transfer.get('bytes') != system.get('size') or transfer.get('sha256') != system.get('sha256') or transfer.get('readback_samples') != 3:
        raise ValueError('completed verified RAM streaming evidence unavailable')
    argv = command['argv']
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        raise ValueError('invalid recorded QEMU argv')
    endpoints = [argv[i+1] for i, arg in enumerate(argv[:-1]) if arg == '-gdb']
    if len(endpoints) != 1:
        raise ValueError('exactly one recorded GDB endpoint required')
    endpoint = re.fullmatch(r'tcp:127\.0\.0\.1:([0-9]{1,5})', endpoints[0])
    if endpoint is None or not 1 <= int(endpoint[1]) <= 65535:
        raise ValueError('GDB endpoint must be literal IPv4 loopback')
    if '-M' not in argv or argv[argv.index('-M')+1] != 'virt,gic-version=3':
        raise ValueError('only the recorded QEMU virt research VM is supported')
    cpus = int(argv[argv.index('-smp')+1])
    if not 1 <= cpus <= 8:
        raise ValueError('CPU count outside capture bound')
    kernel_info = command['inputs']['kernel']
    kernel = Path(kernel_info['path'])
    digest = sha256(kernel)
    if digest != kernel_info['sha256'] or kernel.stat().st_size != kernel_info['size']:
        raise ValueError('recorded kernel input has changed')
    manifest = (artifact / 'kernel.sha256').read_text().strip().splitlines()
    if len(manifest) != 1 or manifest[0].split()[0] != digest:
        raise ValueError('artifact kernel hash does not match recorded VM')
    source = (artifact / 'source-commit.txt').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}', source):
        raise ValueError('invalid artifact source commit')
    symbols_file = artifact / 'kernel.syms'
    if symbols_file.stat().st_size > 32 << 20:
        raise ValueError('oversized symbol file')
    wanted = {'_lck_ticket_timeout_panic', '_pset0'}
    symbols = {}
    for line in symbols_file.read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] in wanted:
            if fields[2] in symbols:
                raise ValueError('duplicate required kernel symbol')
            symbols[fields[2]] = int(fields[0], 16)
    if symbols.keys() != wanted or any(not 0xFFFF000000000000 <= a < U64 for a in symbols.values()):
        raise ValueError('required unshifted ARM64 kernel symbols unavailable')
    callback = symbols['_lck_ticket_timeout_panic']
    if callback % 4 or symbols['_pset0'] % 128:
        raise ValueError('unexpected kernel symbol alignment')
    expected = macho_bytes(kernel, callback, 32)
    return dict(port=int(endpoint[1]), cpus=cpus, callback=callback,
                lock=symbols['_pset0']+0x100, expected=expected,
                provenance=dict(kernel_path=str(kernel.resolve()), kernel_sha256=digest,
                                symbols_path=str(symbols_file.resolve()),
                                symbols_sha256=sha256(symbols_file), source_commit=source,
                                command_sha256=sha256(run / 'command.json')))


def macho_bytes(path, address, size):
    """Map an unshifted VA to a bounded file-backed ARM64 Mach-O segment."""
    with Path(path).open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError('truncated Mach-O header')
        magic, cpu, _, _, ncmds, cmdsize, _, _ = struct.unpack('<8I', header)
        if magic != 0xFEEDFACF or cpu != 0x100000C or not 1 <= ncmds <= 4096 or cmdsize > LIMIT:
            raise ValueError('only bounded little-endian ARM64 Mach-O supported')
        commands = stream.read(cmdsize)
        if len(commands) != cmdsize:
            raise ValueError('truncated Mach-O load commands')
        offset = 0
        matches = []
        for _ in range(ncmds):
            if offset + 8 > len(commands):
                raise ValueError('truncated Mach-O command')
            kind, length = struct.unpack_from('<II', commands, offset)
            if length < 8 or offset+length > len(commands):
                raise ValueError('invalid Mach-O command length')
            if kind == 0x19:
                if length < 72:
                    raise ValueError('truncated Mach-O segment')
                va, _, fileoff, filesize = struct.unpack_from('<4Q', commands, offset+24)
                delta = address-va
                if 0 <= delta <= filesize and size <= filesize-delta:
                    matches.append(fileoff+delta)
            offset += length
        if offset != len(commands) or len(matches) != 1:
            raise ValueError('symbol does not have one file-backed segment')
        stream.seek(matches[0])
        result = stream.read(size)
        if len(result) != size:
            raise ValueError('truncated symbol bytes')
        return result


class RSP:
    """ACK-mode, all-stop RSP with a fixed monotonic deadline."""
    def __init__(self, port, deadline):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('invalid loopback port')
        self.deadline = deadline
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=min(5, self.remaining()))
        self.buffer = bytearray()
        self.packet_size = 4096
        self.stopped = False

    def remaining(self):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('capture deadline exceeded')
        return remaining

    def byte(self):
        while not self.buffer:
            self.sock.settimeout(min(5, self.remaining()))
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                self.remaining()
                continue
            if not data:
                raise RuntimeError('GDB connection closed')
            self.buffer.extend(data)
        return self.buffer.pop(0)

    def receive(self):
        for _ in range(128):
            if self.byte() == 36:
                break
        else:
            raise RuntimeError('missing RSP packet start')
        raw = bytearray()
        while True:
            ch = self.byte()
            if ch == 35:
                break
            raw.append(ch)
            if ch in (125, 42):
                raw.append(self.byte())
            if len(raw) > LIMIT:
                raise RuntimeError('oversized RSP response')
        try:
            checksum = int(bytes((self.byte(), self.byte())), 16)
        except ValueError as exc:
            raise RuntimeError('invalid RSP checksum encoding') from exc
        if sum(raw) & 255 != checksum:
            raise RuntimeError('RSP checksum mismatch')
        self.sock.sendall(b'+')
        return unescape(raw)

    def send(self, payload):
        wire = escape(payload)
        if len(wire) > self.packet_size:
            raise ValueError('RSP request exceeds negotiated packet size')
        frame = b'$'+wire+b'#'+f'{sum(wire)&255:02x}'.encode()
        for _ in range(3):
            self.sock.sendall(frame)
            ack = self.byte()
            if ack == 43:
                return
            if ack != 45:
                raise RuntimeError('invalid RSP acknowledgment')
        raise RuntimeError('RSP request repeatedly rejected')

    def call(self, payload):
        self.send(payload)
        reply = self.receive()
        if reply.startswith(b'E'):
            raise RuntimeError(f'GDB target error: {reply!r}')
        return reply

    def stop_reply(self):
        for _ in range(128):
            reply = self.receive()
            if re.fullmatch(rb'O[0-9a-fA-F]*', reply):
                continue
            if reply[:1] not in (b'S', b'T') or not re.match(rb'^[ST][0-9a-fA-F]{2}', reply):
                raise RuntimeError(f'expected stopped target, got {reply!r}')
            self.stopped = True
            return reply.decode('ascii')
        raise RuntimeError('too many GDB console packets')

    def interrupt(self):
        self.sock.sendall(b'\x03')
        return self.stop_reply()

    def negotiate(self):
        features = self.call(b'qSupported:qXfer:features:read+').decode('ascii').split(';')
        sizes = [int(f.split('=')[1], 16) for f in features if f.startswith('PacketSize=')]
        self.packet_size = sizes[0] if sizes else 4096
        if not 256 <= self.packet_size <= LIMIT or 'qXfer:features:read+' not in features:
            raise RuntimeError('bounded target XML feature transfer unavailable')
        if self.call(b'Qqemu.PhyMemMode:0') != b'OK' or self.call(b'qqemu.PhyMemMode') != b'0':
            raise RuntimeError('QEMU virtual-memory mode 0 could not be verified')

    def read(self, address, size):
        if not self.stopped or not 0 <= address < U64 or not 0 < size <= min(4096, self.packet_size//2) or address+size > U64:
            raise ValueError('invalid paused virtual-memory read')
        raw = self.call(f'm{address:x},{size:x}'.encode())
        if len(raw) != size*2 or not re.fullmatch(b'[0-9a-fA-F]+', raw):
            raise RuntimeError('short or unavailable virtual-memory read')
        return bytes.fromhex(raw.decode())

    def xml(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name):
            raise ValueError('unsafe target XML annex name')
        result = bytearray()
        while True:
            raw = self.call(f'qXfer:features:read:{name}:{len(result):x},{min(4096,self.packet_size-64):x}'.encode())
            if raw[:1] not in (b'm', b'l') or len(raw) <= 1:
                raise RuntimeError('empty or unsupported target XML chunk')
            result.extend(raw[1:])
            if len(result) > LIMIT:
                raise ValueError('oversized target XML')
            if raw[:1] == b'l':
                return bytes(result)

    def threads(self):
        result = []
        reply = self.call(b'qfThreadInfo')
        for _ in range(16):
            if reply == b'l':
                if not result:
                    raise RuntimeError('no CPUs enumerated')
                return result
            if reply[:1] != b'm':
                raise RuntimeError('unsupported CPU thread enumeration')
            for item in reply[1:].decode('ascii').split(','):
                if not re.fullmatch(r'(?:p[0-9a-f]+\.)?[0-9a-f]+', item) or int(item.split('.')[-1], 16) == 0 or item in result:
                    raise ValueError('invalid or duplicate CPU thread ID')
                result.append(item)
            if len(result) > 8:
                raise ValueError('CPU count exceeds capture bound')
            reply = self.call(b'qsThreadInfo')
        raise RuntimeError('unbounded CPU enumeration')

    def registers(self, thread, layout):
        if self.call(f'Hg{thread}'.encode()) != b'OK':
            raise RuntimeError('cannot select CPU')
        result = {}
        for name in CORE_NAMES:
            regnum, bits = layout[name]
            raw = self.call(f'p{regnum:x}'.encode())
            if len(raw) != bits//4 or not re.fullmatch(b'[0-9a-fA-F]+', raw):
                raise RuntimeError(f'unavailable CPU register {name}')
            result[name] = int.from_bytes(bytes.fromhex(raw.decode()), 'little')
        return result

    def resume(self):
        self.send(b'c')
        self.stopped = False

    def cleanup(self, breakpoint_address):
        self.deadline = time.monotonic()+5  # Separate, bounded recovery allowance.
        result = dict(detached=False, execution_resume_requested=False)
        try:
            if not self.stopped:
                self.interrupt()
            if breakpoint_address is not None:
                if self.call(f'z0,{breakpoint_address:x},4'.encode()) != b'OK':
                    raise RuntimeError('breakpoint removal failed')
            if self.call(b'D') != b'OK':
                raise RuntimeError('GDB detach failed')
            result.update(detached=True, execution_resume_requested=True)
        except Exception as exc:
            result['error'] = str(exc)
            try:
                self.resume()
                result['execution_resume_requested'] = True
            except Exception as resume_error:
                result['resume_error'] = str(resume_error)
        finally:
            self.sock.close()
        return result


def register_layout(target):
    """Read includes in document order; explicit GDB regnum can be sparse."""
    layout, documents = {}, {}
    next_reg = 0
    architectures = []

    def load(name, ancestry=()):
        nonlocal next_reg
        if name in ancestry or len(ancestry) >= 8 or len(documents) >= 32:
            raise ValueError('cyclic or excessive XML includes')
        raw = target.xml(name)
        # Built-in declarations are sufficient. Never resolve XML entities.
        if b'<!ENTITY' in raw.upper():
            raise ValueError('XML entities unsupported')
        documents[name] = hashlib.sha256(raw).hexdigest()
        root = ET.fromstring(raw)
        for node in root.iter():
            tag = node.tag.split('}')[-1]
            if tag == 'architecture':
                architectures.append((node.text or '').strip())
            if tag == 'include':
                load(node.attrib['href'], ancestry+(name,))
            elif tag == 'reg':
                number = int(node.attrib.get('regnum', str(next_reg)), 0)
                bits = int(node.attrib['bitsize'])
                regname = node.attrib['name']
                if not 0 <= number <= 65535 or bits <= 0 or bits > 65536 or bits % 8 or regname in layout:
                    raise ValueError('invalid or duplicate XML register')
                layout[regname] = (number, bits)
                next_reg = number+1

    load('target.xml')
    if architectures != ['aarch64']:
        raise ValueError('only explicit little-endian aarch64 target layout supported')
    for name in CORE_NAMES:
        if name not in layout or layout[name][1] != (32 if name == 'cpsr' else 64):
            raise ValueError(f'missing or unsupported core register {name}')
    if len({layout[name][0] for name in layout}) != len(layout):
        raise ValueError('duplicate XML register numbers')
    return layout, documents


def frames(target, registers, count):
    fp = registers['x29']
    result = []
    for _ in range(count):
        if fp == 0:
            return result, 'zero_frame_pointer'
        if fp % 16 or not 0xFFFF000000000000 <= fp < U64-16:
            return result, 'non_kernel_or_unaligned_frame_pointer'
        previous, lr = struct.unpack('<QQ', target.read(fp, 16))
        result.append(dict(fp=hex(fp), previous_fp=hex(previous), lr=hex(lr)))
        if previous and (previous <= fp or previous-fp > 1 << 20):
            return result, 'invalid_or_excessive_frame_step'
        fp = previous
    return result, 'frame_limit'


def capture(target, prepared, frame_count=32):
    """All-stop capture. Caller must run cleanup even when this raises."""
    target.interrupt()
    target.negotiate()
    layout, documents = register_layout(target)
    threads = target.threads()
    if len(threads) != prepared['cpus']:
        raise RuntimeError('enumerated CPU count differs from recorded QEMU')
    target.registers(threads[0], layout)
    if target.read(prepared['callback'], len(prepared['expected'])) != prepared['expected']:
        raise RuntimeError('runtime callback bytes differ: wrong kernel, slide, or unavailable mapping')
    if target.call(f'Z0,{prepared["callback"]:x},4'.encode()) != b'OK':
        raise RuntimeError('pre-panic software breakpoint unsupported')
    target.resume()
    stop = target.stop_reply()
    if stop[1:3] != '05':
        raise RuntimeError('target stop was not a breakpoint trap')
    cpus = []
    for thread in threads:
        regs = target.registers(thread, layout)
        stack, end = frames(target, regs, frame_count)
        cpus.append(dict(thread=thread, registers={k: hex(v) for k,v in regs.items()},
                         frames=stack, frame_end=end))
    if not any(int(cpu['registers']['pc'], 16) == prepared['callback'] for cpu in cpus):
        raise RuntimeError('target stopped away from expected timeout callback')
    # Kernel mappings should be shared, but select the CPU that hit before reading.
    hitting = next(cpu for cpu in cpus if int(cpu['registers']['pc'], 16) == prepared['callback'])
    target.call(f'Hg{hitting["thread"]}'.encode())
    lock = target.read(prepared['lock'], 16)
    return dict(status='diagnostic_capture', incomplete=False, stop_reply=stop,
                breakpoint=hex(prepared['callback']), lock_address=hex(prepared['lock']),
                lock_bytes=lock.hex(), cpus=cpus, target_xml_sha256=documents,
                register_layout={k: dict(regnum=n,bitsize=b) for k,(n,b) in layout.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--artifact', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--wait-seconds', type=int, default=600)
    parser.add_argument('--frames', type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 1800 or not 1 <= args.frames <= 64:
        parser.error('wait must be 1..1800 seconds and frames 1..64')
    # Exclusive creation protects previous evidence. Parents must already exist.
    with args.output.open('x', encoding='utf-8') as output:
        result = dict(status='incomplete', incomplete=True, boot_test_pass=False)
        target = None
        prepared = None
        try:
            prepared = prepare(args.run, args.artifact)
            result['provenance'] = prepared['provenance']
            target = RSP(prepared['port'], time.monotonic()+args.wait_seconds)
            result.update(capture(target, prepared, args.frames))
        except Exception as exc:
            result.update(status='incomplete', incomplete=True, error=f'{type(exc).__name__}: {exc}')
        finally:
            if target is not None:
                result['cleanup'] = target.cleanup(prepared['callback'])
                if not result['cleanup'].get('detached'):
                    result.update(status='incomplete', incomplete=True)
            output.write(json.dumps(result, indent=2)+'\n')
        print(json.dumps({k:result[k] for k in ('status','incomplete','boot_test_pass')}))
        return 1 if result['incomplete'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
