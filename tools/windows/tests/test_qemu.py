import argparse
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_qemu import command, console_evidence, snapshot, guest_started


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
    def args(self, system=None, network=False, control_port=0):
        return argparse.Namespace(qemu=Path('qemu.exe'), efi=Path('fw/code.fd'),
                                  cpus=4, memory_gib=7, network=network,
                                  control_port=control_port, system=system)

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

    def test_system_must_fit_ram(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'system.apfs'
            image.write_bytes(b'fixture')
            args = self.args(system=image)
            args.memory_gib = 1
            with self.assertRaisesRegex(ValueError, 'fit guest RAM'):
                command(args, Path(tmp), 4445)

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


if __name__ == '__main__':
    unittest.main()
