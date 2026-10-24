#!/usr/bin/env python3
"""Bounded Windows QEMU run; records inputs, console, QMP screenshot and result.

This script only starts an emulator. It has no USB/fastboot or handset code.
Use --ramdisk with the small APFS image and --system with the iOS system image.
Without --system a standalone static Mach-O can exercise mockfs/PID 1 only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from gdb_memory import GDBMemory

ROOT = Path(__file__).resolve().parents[2]
RD_ADDR = 0xFC000000
LOG_LIMIT = 64 << 20


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class QMP:
    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=3)
        self.stream = self.sock.makefile('rwb')
        try:
            if 'QMP' not in self.receive():
                raise RuntimeError('invalid QMP greeting')
            self.call('qmp_capabilities')
        except Exception:
            self.close()
            raise

    def receive(self):
        line = self.stream.readline(1 << 20)
        if not line or len(line) >= 1 << 20:
            raise RuntimeError('QMP closed/oversized response')
        return json.loads(line)

    def call(self, execute, arguments=None):
        request = {'execute': execute}
        if arguments is not None:
            request['arguments'] = arguments
        self.stream.write(json.dumps(request).encode() + b'\n')
        self.stream.flush()
        for _ in range(100):
            response = self.receive()
            if 'error' in response:
                raise RuntimeError(response['error'])
            if 'return' in response:
                return response['return']
        raise RuntimeError('too many QMP events')

    def close(self):
        try:
            self.stream.close()
        finally:
            self.sock.close()


def snapshot(monitor, output, elapsed=None):
    # Native PNG avoids this Windows QEMU build's padded PPM rows at the
    # iPhone's odd 1125-pixel width. Keep QEMU's pixels unchanged.
    monitor.call('screendump', {'filename': (output / 'screen.png').as_posix(), 'format': 'png'})
    regs = monitor.call('human-monitor-command', {'command-line': 'info registers -a'})
    (output / 'registers.txt').write_text(regs)
    if elapsed is not None:
        history = output / 'snapshots'
        history.mkdir(exist_ok=True)
        stem = f'{elapsed:07.1f}'
        shutil.copyfile(output / 'screen.png', history / (stem + '.png'))
        (history / (stem + '.txt')).write_text(regs)


def console_evidence(path):
    """Record narrow observations; a normal loader return is not a GUI pass.

    The loader replays the kernel log on return. Counts from this stream would
    double-count guest events, so report presence only.
    """
    text = path.read_bytes().decode('utf-8', errors='replace')
    return {
        'loader_returned': 'kernel returned: log at ' in text,
        'early_exception': 'EARLY KERNEL EXCEPTION:' in text,
        'panic_marker': 'panic(cpu ' in text or 'panic: ' in text,
        'init_checks_passed': '=== 36 checks, 0 failed ===' in text,
        'surface_checks_passed': 'surf_test: 80 checks, 0 failed' in text,
    }


def guest_started(path):
    # Only the bounded prefix: the banner precedes userland's large logs.
    with path.open('rb') as stream:
        return b'Darwin Kernel Version ' in stream.read(64 << 10)


def boot_arguments(args):
    """Build QEMU-only arguments; thermal requests synthetic samples, never phone MMIO."""
    values = [f'loader_return_after={args.seconds}', f'epoch={int(time.time())}',
              'virtio_input=1', f'cpus={args.cpus}', 'tlto_us=25000000']
    if args.network:
        values.append('virtio_net=1')
    if getattr(args, 'wifi', False):
        values.append('wifi_qemu=1')
    if getattr(args, 'charging', False):
        values.append('battery=1')
    if getattr(args, 'thermal', False):
        values.append('thermal=1')
    return ' '.join(values)


def command(args, output, port, gdb_port=None):
    cmd = [str(args.qemu), '-M', 'virt,gic-version=3', '-cpu', 'cortex-a76',
           '-smp', str(args.cpus), '-m', f'{args.memory_gib}G',
           '-accel', 'tcg,tb-size=192', '-L', str(args.efi.parent),
           '-drive', f'if=pflash,format=raw,readonly=on,file={args.efi.as_posix()}',
           '-drive', f'if=pflash,format=raw,file={(output / "vars.fd").as_posix()}',
           '-drive', f'file=fat:rw:{(output / "esp").as_posix()},format=raw,if=virtio',
           '-device', f'loader,file={(output / "payload.bin").as_posix()},addr=0xf0000000,force-raw=on',
           '-global', 'virtio-mmio.force-legacy=false', '-device', 'virtio-tablet-device',
           '-device', 'virtio-keyboard-device', '-device', 'ramfb',
           '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
           '-nic', 'none',
           '-qmp', f'tcp:127.0.0.1:{port},server=on,wait=off']
    if args.network:
        cmd += ['-netdev', 'user,id=net0', '-device', 'virtio-net-device,netdev=net0']
    if args.control_port:
        # Separate native QMP monitor for input/diagnostics while capture owns
        # its connection. Two clients cannot share a single socket monitor.
        cmd += ['-qmp', f'tcp:127.0.0.1:{args.control_port},server=on,wait=off']
    if args.system:
        if RD_ADDR + args.system.stat().st_size > 0x40000000 + (args.memory_gib << 30):
            raise ValueError('system image does not fit guest RAM')
        if getattr(args, 'system_transfer', 'loader') == 'gdb':
            if gdb_port is None:
                raise ValueError('RAM streaming requires a separate GDB port')
            cmd += ['-S', '-gdb', f'tcp:127.0.0.1:{gdb_port}']
        else:
            # Diagnostic fallback: Windows raw loader keeps another multi-GiB ROM copy.
            cmd += ['-device', f'loader,file={args.system.as_posix()},addr={RD_ADDR:#x},force-raw=on']
    return cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--efi', type=Path, required=True)
    parser.add_argument('--vars', type=Path, required=True)
    parser.add_argument('--ramdisk', type=Path, required=True)
    parser.add_argument('--loader', type=Path, default=ROOT / 'prebuilt/loader.efi')
    parser.add_argument('--kernel', type=Path, default=ROOT / 'prebuilt/mach.development.bcm2837',
                        help='explicit rebuilt kernel, otherwise upstream prebuilt')
    parser.add_argument('--system', type=Path)
    parser.add_argument('--system-transfer', choices=('gdb', 'loader'), default='gdb',
                        help='stream to paused guest RAM (default), or use the memory-heavy raw loader')
    parser.add_argument('--network', action='store_true', help='QEMU NAT + emulated virtio-net (not phone Wi-Fi)')
    parser.add_argument('--wifi', action='store_true',
                        help='synthetic Wi-Fi station "HackiOS-QEMU" over virtio-net (emulation, not WCN3990); needs --network')
    parser.add_argument('--charging', action='store_true',
                        help='QEMU synthetic battery on a charger (emulation, not the phone fuel gauge)')
    parser.add_argument('--thermal', action='store_true',
                        help='enable QEMU synthetic thermal samples; does not access sweet MMIO')
    parser.add_argument('--control-port', type=int, default=0, help='optional second loopback QMP monitor')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--host-grace', type=int, default=40,
                        help='seconds after the guest budget before stopping QEMU')
    parser.add_argument('--startup-seconds', type=int, default=600,
                        help='separate bound for RAM preload/UEFI before the kernel starts')
    parser.add_argument('--cpus', type=int, default=1, choices=range(1, 5))
    parser.add_argument('--memory-gib', type=int, default=7, choices=range(5, 9))
    args = parser.parse_args()
    if not 15 <= args.seconds <= 7200:
        parser.error('seconds must be 15..7200')
    if not 0 <= args.host_grace <= 600:
        parser.error('host-grace must be 0..600')
    if not 15 <= args.startup_seconds <= 600:
        parser.error('startup-seconds must be 15..600')
    if args.control_port and not 1024 <= args.control_port <= 65535:
        parser.error('control-port must be 1024..65535')
    if args.wifi and not args.network:
        parser.error('--wifi needs --network')
    for name in ('qemu', 'efi', 'vars', 'ramdisk', 'system', 'loader', 'kernel'):
        path = getattr(args, name)
        if path:
            path = path.resolve(strict=True)
            if not path.is_file():
                parser.error(f'{name} is not a file')
            setattr(args, name, path)
    output = args.output.resolve()
    if not str(output).isascii() or ',' in str(output):
        parser.error('output must be an ASCII path without commas')
    for path in (args.efi, args.vars, args.system):
        if path and (not str(path).isascii() or ',' in str(path)):
            parser.error('firmware/system paths must be ASCII without commas')
    output.mkdir(parents=True, exist_ok=False)
    efi_boot = output / 'esp/EFI/BOOT'
    efi_boot.mkdir(parents=True)
    shutil.copyfile(args.loader, efi_boot / 'BOOTAA64.EFI')
    shutil.copyfile(args.vars, output / 'vars.fd')
    kernel = args.kernel
    # The current kernel does not consume the loader's device-tree
    # ticket-lock-scale property.  tlto_us is read by ml_init_lock_timeout()
    # and gives the ticket lock the intended QEMU-only 25 seconds; without it
    # a host-descheduled vCPU can hit the 20x global timeout (60,000,000 ticks).
    payload = [sys.executable, str(ROOT / 'xnuloader/mkpayload.py'), '-o',
               str(output / 'payload.bin'), f'kernel={kernel}', f'ramdisk={args.ramdisk}',
               '--cmdline', boot_arguments(args)]
    if args.system:
        payload += ['--rdres', f'{args.system.stat().st_size:x}']
    subprocess.run(payload, check=True)
    inputs = {name: {'path': str(path), 'size': path.stat().st_size, 'sha256': digest(path)}
              for name, path in dict(kernel=kernel, loader=args.loader,
                                    ramdisk=args.ramdisk, system=args.system,
                                    firmware=args.efi, variables=args.vars).items() if path}
    port = free_port()
    while port == args.control_port:
        port = free_port()
    gdb_port = None
    if args.system and args.system_transfer == 'gdb':
        gdb_port = free_port()
        while gdb_port in (port, args.control_port):
            gdb_port = free_port()
    cmd = command(args, output, port, gdb_port)
    (output / 'command.json').write_text(json.dumps(dict(argv=cmd, inputs=inputs), indent=2))
    result = dict(mode='full_ios_attempt' if args.system else 'static_pid1_only',
                  status='started', screenshot=False, log_limit=False,
                  stop_reason=None, host_terminated=False)
    started = time.monotonic()
    deadline = started + args.startup_seconds
    process = None
    monitor = None
    next_snapshot = 20
    try:
        with (output / 'console.log').open('wb') as log:
            process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if gdb_port is not None:
                while True:
                    try:
                        memory = GDBMemory(gdb_port)
                        break
                    except ConnectionRefusedError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise RuntimeError('QEMU RAM preload endpoint did not start')
                        time.sleep(0.5)
                try:
                    def progress(done, size):
                        (output / 'progress.json').write_text(json.dumps(dict(
                            result, status='loading_system', system_bytes_loaded=done,
                            system_bytes=size, host_seconds=round(time.monotonic()-started, 2)), indent=2))
                        print(f'guest RAM: {done}/{size} bytes', flush=True)
                    result['system_transfer'] = memory.load(args.system, RD_ADDR, deadline, progress)
                    if result['system_transfer']['sha256'] != inputs['system']['sha256']:
                        raise RuntimeError('system image changed between hashing and RAM preload')
                    memory.detach()
                finally:
                    memory.close()
            while process.poll() is None and time.monotonic() < deadline:
                if 'kernel_started_host_seconds' not in result and guest_started(output / 'console.log'):
                    result['kernel_started_host_seconds'] = round(time.monotonic() - started, 2)
                    deadline = time.monotonic() + args.seconds + args.host_grace
                if monitor is None:
                    try:
                        monitor = QMP(port)
                    except (OSError, RuntimeError):
                        pass
                if (output / 'console.log').stat().st_size >= LOG_LIMIT:
                    result['log_limit'] = True
                    result['stop_reason'] = 'log_limit'
                    break
                if monitor and time.monotonic() - started >= next_snapshot:
                    snapshot(monitor, output, time.monotonic() - started)
                    result['screenshot'] = True
                    (output / 'progress.json').write_text(json.dumps(
                        dict(result, status='running', host_seconds=round(time.monotonic() - started, 2)), indent=2))
                    next_snapshot = time.monotonic() - started + 20
                time.sleep(0.5)
            if monitor and process.poll() is None:
                snapshot(monitor, output, time.monotonic() - started)
                result['screenshot'] = True
            if result['stop_reason'] is None:
                result['stop_reason'] = ('process_exit' if process.poll() is not None else
                                         'host_deadline' if 'kernel_started_host_seconds' in result else
                                         'startup_deadline')
            result['status'] = 'captured' if result['screenshot'] else 'exited_without_snapshot'
    except Exception as error:
        result.update(status='host_error', stop_reason='host_error', error=str(error))
        raise
    finally:
        if monitor:
            try:
                monitor.close()
            except OSError as error:
                result['monitor_close_error'] = str(error)
        if process:
            if process.poll() is None:
                result['host_terminated'] = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            result['exit_code'] = process.returncode
        result['host_seconds'] = round(time.monotonic() - started, 2)
        log_path = output / 'console.log'
        if log_path.exists():
            result['console_evidence'] = console_evidence(log_path)
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


if __name__ == '__main__':
    main()
