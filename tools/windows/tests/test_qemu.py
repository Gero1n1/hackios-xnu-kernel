import argparse
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_qemu import boot_arguments, command, console_evidence, snapshot, guest_started


class Monitor:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'screendump':
            Path(arguments['filename']).write_bytes(b'PNG fixture')
        else:
            return 'CPU#0\nCPU#1\n'


class QemuTests(unittest.TestCase):
    def args(self, system=None, network=False, control_port=0, thermal=False):
        return argparse.Namespace(qemu=Path('qemu.exe'), efi=Path('fw/code.fd'),
                                  cpus=4, memory_gib=7, network=network,
                                  control_port=control_port, system=system,
                                  seconds=120, thermal=thermal)

    def test_qemu_thermal_flag_is_opt_in_and_synthetic(self):
        self.assertNotIn('thermal=1', boot_arguments(self.args()))
        enabled = boot_arguments(self.args(thermal=True))
        self.assertIn('thermal=1', enabled)
        self.assertIn('tlto_us=25000000', enabled)

    def test_local_monitors_and_no_implicit_network(self):
        cmd = command(self.args(control_port=4446), Path('capture'), 4445)
        self.assertEqual(cmd.count('-qmp'), 2)
        for value in (cmd[i+1] for i, item in enumerate(cmd) if item == '-qmp'):
            self.assertTrue(value.startswith('tcp:127.0.0.1:'))
        self.assertEqual(cmd[cmd.index('-nic') + 1], 'none')
        self.assertNotIn('-netdev', cmd)

    def test_network_is_explicit_and_emulated(self):
        cmd = command(self.args(network=True), Path('capture'), 4445)
        self.assertIn('user,id=net0', cmd)
        self.assertIn('virtio-net-device,netdev=net0', cmd)

    def test_wifi_and_charging_are_opt_in_emulation(self):
        values = boot_arguments(self.args(network=True))
        self.assertNotIn('wifi_qemu=1', values)
        self.assertNotIn('battery=1', values)
        args = self.args(network=True)
        args.wifi = True
        args.charging = True
        values = boot_arguments(args)
        self.assertIn('virtio_net=1', values)
        self.assertIn('wifi_qemu=1', values)
        self.assertIn('battery=1', values)

    def test_system_must_fit_ram(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'system.apfs'
            image.write_bytes(b'fixture')
            args = self.args(system=image)
            args.memory_gib = 1
            with self.assertRaisesRegex(ValueError, 'fit guest RAM'):
                command(args, Path(tmp), 4445)

    def test_streaming_pauses_cpu_and_avoids_system_rom_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'system.apfs'
            path.write_bytes(b'fixture')
            args = self.args(system=path)
            args.system_transfer = 'gdb'
            with self.assertRaisesRegex(ValueError, 'separate GDB port'):
                command(args, Path(tmp), 4445)
            cmd = command(args, Path(tmp), 4445, 4447)
            self.assertIn('-S', cmd)
            self.assertEqual(cmd[cmd.index('-gdb')+1], 'tcp:127.0.0.1:4447')
            self.assertFalse(any('loader,file=' + path.as_posix() in s for s in cmd))

    def test_snapshots_preserve_pixels_and_all_cpu_registers(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            monitor = Monitor()
            snapshot(monitor, output, 42.5)
            self.assertEqual((output / 'screen.png').read_bytes(), b'PNG fixture')
            self.assertEqual((output / 'snapshots/00042.5.png').read_bytes(), b'PNG fixture')
            self.assertEqual((output / 'snapshots/00042.5.txt').read_text(), 'CPU#0\nCPU#1\n')
            self.assertEqual(monitor.calls[0][1]['format'], 'png')
            self.assertEqual(monitor.calls[1][1]['command-line'], 'info registers -a')

    def test_console_replay_is_not_a_second_boot_or_gui_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'console.log'
            fragment = '=== 36 checks, 0 failed ===\nsurf_test: 80 checks, 0 failed\n'
            log.write_text(fragment + 'kernel returned: log at 123, 64 bytes\n' + fragment)
            evidence = console_evidence(log)
            self.assertTrue(evidence['loader_returned'])
            self.assertTrue(evidence['init_checks_passed'])
            self.assertTrue(evidence['surface_checks_passed'])
            self.assertFalse(evidence['panic_marker'])
            self.assertNotIn('gui_pass', evidence)
            log.write_text('panic(cpu 1 caller 0x123): failure\nEARLY KERNEL EXCEPTION: esr=1\n')
            evidence = console_evidence(log)
            self.assertTrue(evidence['panic_marker'])
            self.assertTrue(evidence['early_exception'])

    def test_kernel_budget_starts_after_preload_and_uefi(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'console.log'
            log.write_bytes(b'UEFI firmware\n')
            self.assertFalse(guest_started(log))
            log.write_bytes(b'UEFI firmware\nDarwin Kernel Version 22.5.0: fixture\n')
            self.assertTrue(guest_started(log))
            log.write_bytes(b'x' * (64 << 10) + b'Darwin Kernel Version ')
            self.assertFalse(guest_started(log))

    def test_qemu_loader_scales_only_the_ticket_lock(self):
        root = Path(__file__).resolve().parents[3]
        source = (root / 'xnuloader/loader.c').read_text(encoding='utf-8')
        qemu = source.split('platform_qemu_virt = {', 1)[1].split('};', 1)[0]
        redwood = source.split('platform_redwood = {', 1)[1].split('};', 1)[0]
        qemu_scale = re.search(
            r'\.timeout_scale\s*=\s*(\d+)\s*,\s*\.ticket_timeout_scale\s*=\s*(\d+)', qemu)
        self.assertIsNotNone(qemu_scale)
        self.assertGreaterEqual(int(qemu_scale.group(1)), 20)
        self.assertGreaterEqual(int(qemu_scale.group(2)), 10)
        redwood_scale = re.search(r'\.timeout_scale\s*=\s*(\d+)', redwood)
        self.assertTrue(redwood_scale is None or int(redwood_scale.group(1)) == 0)
        self.assertNotIn('.ticket_timeout_scale', redwood)
        self.assertIn('dt_prop_u32(&w, "ticket-lock-scale"', source)

    def test_qemu_runner_sets_the_effective_ticket_timeout(self):
        root = Path(__file__).resolve().parents[3]
        source = (root / 'tools/windows/run_qemu.py').read_text(encoding='utf-8')
        self.assertIn('tlto_us=25000000', source)
        self.assertIn("'--host-grace'", source)

    def test_qemu_runner_allows_long_tcg_boots(self):
        root = Path(__file__).resolve().parents[3]
        source = (root / 'tools/windows/run_qemu.py').read_text(encoding='utf-8')
        match = re.search(r"15 <= args\.seconds <= (\d+)", source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 3600)


if __name__ == '__main__':
    unittest.main()
