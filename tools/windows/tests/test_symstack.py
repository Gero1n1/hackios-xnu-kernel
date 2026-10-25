import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('guest_symstack', Path(__file__).resolve().parents[3] / 'qemu/symstack.py')
sym = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sym)


class SymbolTests(unittest.TestCase):
    def test_each_process_header_supplies_its_own_slide(self):
        lines = ['ustacks:  thread 0x1: 0x180000100\n',
                 'ustacks: First[1] 1 threads, shared cache slide 0x1000\n',
                 'ustacks:  thread 0x1: 0x180001100 0xdead 0x180001200\n',
                 'ustacks: Second[2] 1 threads, shared cache slide 0x2000\n',
                 'ustacks:  thread 0x2 (running): 0x180002300 0xbeef 0x180002400\n']
        frames = [r for r in sym.records(lines) if isinstance(r, tuple)]
        self.assertEqual(frames, [(0x180001100, 0x1000), (0x180001200, 0x1000),
                                  (0x180002300, 0x2000), (0x180002400, 0x2000)])
        self.assertEqual([r for r in sym.records(lines, 'Second') if isinstance(r, tuple)], frames[2:])

    def test_replay_skipped_and_new_boot_drops_previous_slide(self):
        lines = ['ustacks: First[1] shared cache slide 0x1000\n',
                 'Darwin Kernel Version new boot\n',
                 'ustacks:  thread 0x1: 0x180001100\n',
                 'ustacks: First[1] shared cache slide 0x2000\n',
                 'ustacks:  thread 0x2: 0x180002300\0\n',
                 'kernel returned: log at 123, 456 bytes\n',
                 'ustacks:  thread 0x2: 0x180002300\n']
        self.assertEqual([r for r in sym.records(lines) if isinstance(r, tuple)], [(0x180002300, 0x2000)])

    def test_abort_has_its_own_slide(self):
        lines = ['(0x180001100 0x180001200)\n',
                 'userabort: Test[3] aborts, shared cache slide 0x1000; called from: 0x180001300\n']
        self.assertEqual([r for r in sym.records(lines) if isinstance(r, tuple)],
                         [(0x180001300, 0x1000), (0x180001100, 0x1000), (0x180001200, 0x1000)])
        self.assertEqual(list(sym.records(lines, 'Other')), [])

    def test_identity_covers_cache_and_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'cache'
            symbols = Path(str(cache) + '.symbols')
            cache.write_bytes(b'fixture cache'); symbols.write_bytes(b'fixture symbols')
            original = sym.cache_identity(cache)
            symbols.write_bytes(b'different symbols')
            self.assertNotEqual(original, sym.cache_identity(cache))

    def test_only_success_is_cached_for_exact_input_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            known = Path(tmp) / 'known.json'
            identity = dict(cache='fixture1', symbols='fixture2')
            resolver = sym.Resolver('ipsw', 'cache', known, identity)
            reply = subprocess.CompletedProcess([], 0, b'0x180000100: _test + 4\n', b'')
            with patch.object(sym.subprocess, 'run', return_value=reply) as call:
                self.assertEqual(resolver.name(0x180001100, 0x1000), '0x180000100: _test + 4')
                self.assertEqual(resolver.name(0x180002100, 0x2000), '0x180000100: _test + 4')
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.kwargs['timeout'], 45)
                self.assertTrue(call.call_args.kwargs['check'])
            self.assertEqual(sym.load_known(known, identity), {'0x180000100': '_test + 4'})
            with self.assertRaisesRegex(ValueError, 'identity'):
                sym.load_known(known, dict(cache='other', symbols='fixture2'))
            self.assertEqual(json.loads(known.read_text())['identity'], identity)

    def test_failed_missing_or_exhausted_lookup_does_not_cache_question_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ('failure', 'missing', 'bound'):
                known = Path(tmp) / mode
                resolver = sym.Resolver('ipsw', 'cache', known, {}, max_lookups=0 if mode == 'bound' else 1)
                effect = subprocess.CalledProcessError(1, 'ipsw') if mode == 'failure' else None
                reply = subprocess.CompletedProcess([], 0, b'no symbol\n', b'')
                with patch.object(sym.subprocess, 'run', side_effect=effect, return_value=reply):
                    with self.assertRaises((subprocess.CalledProcessError, RuntimeError)):
                        resolver.name(0x180000100, 0)
                self.assertFalse(known.exists())
                self.assertEqual(resolver.known, {})
            resolver = sym.Resolver('ipsw', 'cache', Path(tmp)/'outside', {})
            self.assertIn('not in the shared cache', resolver.name(0x1000, 0x2000))


if __name__ == '__main__':
    unittest.main()
