import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import smp_capture as smp
from gdb_memory import escape

CALLBACK = 0xFFFFFFF007010000
LOCK = 0xFFFFFFF007020100


def xml_core():
    return ('<feature name="org.gnu.gdb.aarch64.core">' + ''.join(
        f'<reg name="{name}" bitsize="{32 if name == "cpsr" else 64}" regnum="{number}"/>'
        for number, name in enumerate(smp.CORE_NAMES)) + '</feature>').encode()


class Target:
    """RSP-command fixture; no VM or remote device is accessed."""
    def __init__(self):
        self.commands = []
        self.stopped = False
        self.selected = '1'
        self.hit = False
        self.unmapped = False
        self.memory = {CALLBACK: b'K'*32, LOCK: bytes(range(16)),
                       0xFFFFFFF010000000: struct.pack('<QQ', 0, CALLBACK+4)}

    def interrupt(self):
        self.stopped = True
        return 'T02thread:1;'

    def negotiate(self):
        self.commands.extend([b'Qqemu.PhyMemMode:0', b'qqemu.PhyMemMode'])

    def xml(self, name):
        if name == 'target.xml':
            return b'<target xmlns:xi="http://www.w3.org/2001/XInclude"><architecture>aarch64</architecture><xi:include href="core.xml"/></target>'
        return xml_core()

    def threads(self):
        return ['1', '2', '3', '4']

    def registers(self, thread, layout):
        self.selected = thread
        result = {k: 0 for k in smp.CORE_NAMES}
        result['pc'] = CALLBACK if self.hit and thread == '3' else CALLBACK+4
        result['x29'] = 0xFFFFFFF010000000
        return result

    def read(self, address, size):
        if self.unmapped:
            raise RuntimeError('GDB target error: E14')
        return self.memory[address][:size]

    def call(self, command):
        self.commands.append(command)
        if command.startswith(b'Hg'):
            self.selected = command[2:].decode()
        return b'OK'

    def resume(self):
        self.stopped = False

    def stop_reply(self):
        self.stopped, self.hit = True, True
        return 'T05thread:3;'


class WireSocket:
    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = []
        self.closed = False

    def recv(self, size):
        if not self.data:
            return b''
        return bytes([self.data.pop(0)])  # Deliberate transport fragmentation.

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        if timeout <= 0:
            raise AssertionError('unbounded timeout')

    def close(self):
        self.closed = True


def packet(data):
    wire = escape(data)
    return b'$'+wire+b'#'+f'{sum(wire)&255:02x}'.encode()


def rsp(data=b''):
    target = smp.RSP.__new__(smp.RSP)
    target.sock = WireSocket(data)
    target.buffer = bytearray()
    target.packet_size = 4096
    target.deadline = time.monotonic()+1
    target.stopped = True
    return target


class SMPCaptureTests(unittest.TestCase):
    def prepared(self):
        return dict(cpus=4, callback=CALLBACK, lock=LOCK, expected=b'K'*32)

    def test_target_xml_include_explicit_register_layout(self):
        layout, documents = smp.register_layout(Target())
        self.assertEqual(layout['pc'], (32,64))
        self.assertEqual(layout['cpsr'], (33,32))
        self.assertEqual(set(documents), {'target.xml','core.xml'})

    def test_bad_xml_is_rejected(self):
        cases = [b'<target><architecture>arm</architecture></target>',
                 b'<target/>', b'<!DOCTYPE a [<!ENTITY b "x">]><target/>',
                 b'<target xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="target.xml"/></target>',
                 b'<target xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="../core.xml"/></target>']
        for data in cases:
            with self.subTest(data=data):
                target = Target()
                def xml(name):
                    if '/' in name:
                        raise ValueError('unsafe annex')
                    return data
                target.xml = xml
                with self.assertRaises((ValueError, KeyError)):
                    smp.register_layout(target)

    def test_duplicate_and_bad_width_registers_rejected(self):
        for data in [xml_core().replace(b'name="pc"', b'name="x0"'),
                     xml_core().replace(b'name="pc" bitsize="64"', b'name="pc" bitsize="32"'),
                     xml_core().replace(b'regnum="32"',b'regnum="0"')]:
            target = Target()
            original = target.xml
            target.xml = lambda name: data if name == 'core.xml' else original(name)
            with self.assertRaises(ValueError):
                smp.register_layout(target)

    def test_capture_all_cpus_lock_and_frames_at_callback(self):
        target = Target()
        result = smp.capture(target, self.prepared())
        self.assertEqual(result['status'], 'diagnostic_capture')
        self.assertFalse(result['incomplete'])
        self.assertEqual(len(result['cpus']), 4)
        self.assertEqual(result['lock_bytes'], bytes(range(16)).hex())
        self.assertTrue(all(cpu['frame_end']=='zero_frame_pointer' for cpu in result['cpus']))
        self.assertEqual(target.selected, '3')
        self.assertIn(f'Z0,{CALLBACK:x},4'.encode(),target.commands)
        self.assertFalse(any(command[:1] in (b'M',b'X') for command in target.commands))

    def test_wrong_kernel_or_unmapped_virtual_memory_is_failure(self):
        for unmapped in (True,False):
            target = Target()
            target.unmapped = unmapped
            target.memory[CALLBACK] = b'wrong'
            with self.assertRaisesRegex(RuntimeError, 'target error|runtime callback'):
                smp.capture(target, self.prepared())
            self.assertFalse(any(c.startswith(b'Z0') for c in target.commands))

    def test_other_stop_or_cpu_count_is_failure(self):
        target = Target()
        target.threads = lambda: ['1']
        with self.assertRaisesRegex(RuntimeError,'CPU count'):
            smp.capture(target,self.prepared())
        target = Target()
        target.stop_reply = lambda: 'T02thread:3;'
        with self.assertRaisesRegex(RuntimeError,'breakpoint trap'):
            smp.capture(target,self.prepared())
        target = Target()
        original = target.registers
        def wrong_pc(thread,layout):
            result=original(thread,layout);result['pc']=CALLBACK+4;return result
        target.registers=wrong_pc
        with self.assertRaisesRegex(RuntimeError,'away from expected'):
            smp.capture(target,self.prepared())

    def test_frame_chain_limits_and_invalid_pointer(self):
        target=Target()
        base=0xFFFFFFF010000000
        target.memory[base]=struct.pack('<QQ',base,CALLBACK)
        frames,end=smp.frames(target,{'x29':base},32)
        self.assertEqual(end,'invalid_or_excessive_frame_step')
        self.assertEqual(len(frames),1)
        self.assertEqual(smp.frames(target,{'x29':1},32),([], 'non_kernel_or_unaligned_frame_pointer'))
        self.assertEqual(smp.frames(target,{'x29':base},1)[1],'invalid_or_excessive_frame_step')

    def test_rsp_fragmented_ack_retry_and_checksum(self):
        target=rsp(b'-+'+packet(b'OK'))
        self.assertEqual(target.call(b'fixture'),b'OK')
        self.assertEqual(target.sock.sent[0],target.sock.sent[1])
        self.assertEqual(target.sock.sent[-1],b'+')
        with self.assertRaisesRegex(RuntimeError,'checksum'):
            rsp(b'+$OK#00').call(b'fixture')
        with self.assertRaisesRegex(RuntimeError,'closed'):
            rsp(b'+$truncated').call(b'fixture')

    def test_rsp_errors_deadline_and_mode_validation(self):
        with self.assertRaisesRegex(RuntimeError,'target error'):
            rsp(b'+'+packet(b'E14')).call(b'm0,1')
        target=rsp()
        target.deadline=time.monotonic()-1
        with self.assertRaises(TimeoutError):
            target.byte()
        features=b'PacketSize=1000;qXfer:features:read+'
        target=rsp(b'+'+packet(features)+b'+'+packet(b'OK')+b'+'+packet(b'1'))
        with self.assertRaisesRegex(RuntimeError,'virtual-memory mode'):
            target.negotiate()
        self.assertFalse(any(b'PhyMemMode:1' in wire for wire in target.sock.sent))

    def test_rsp_memory_registers_and_threads(self):
        target=rsp(b'+'+packet(b'm1,2')+b'+'+packet(b'l'))
        self.assertEqual(target.threads(),['1','2'])
        target=rsp(b'+'+packet(b'm1,1'))
        with self.assertRaisesRegex(ValueError,'duplicate'):
            target.threads()
        self.assertEqual(rsp(b'+'+packet(b'1234')).read(CALLBACK,2),b'\x12\x34')
        with self.assertRaisesRegex(RuntimeError,'short'):
            rsp(b'+'+packet(b'xx')).read(CALLBACK,1)
        layout,_=smp.register_layout(Target())
        values=b'+'+packet(b'OK')
        for name in smp.CORE_NAMES:
            values+=b'+'+packet((123).to_bytes(layout[name][1]//8,'little').hex().encode())
        self.assertEqual(rsp(values).registers('1',layout)['pc'],123)

    def test_rsp_stop_console_and_xml_bounds(self):
        target=rsp(packet(b'O6869')+packet(b'T05thread:1;'))
        self.assertTrue(target.stop_reply().startswith('T05'))
        target=rsp(b'+'+packet(b'm<tar')+b'+'+packet(b'lget/>'))
        self.assertEqual(target.xml('target.xml'),b'<target/>')
        with self.assertRaisesRegex(ValueError,'unsafe'):
            rsp().xml('../outside')
        with self.assertRaisesRegex(RuntimeError,'empty'):
            rsp(b'+'+packet(b'm')).xml('target.xml')

    def test_cleanup_removes_breakpoint_and_detaches(self):
        target=rsp(b'+'+packet(b'OK')+b'+'+packet(b'OK'))
        result=target.cleanup(CALLBACK)
        self.assertTrue(result['detached'])
        self.assertTrue(result['execution_resume_requested'])
        self.assertTrue(target.sock.closed)
        self.assertTrue(any(f'z0,{CALLBACK:x},4'.encode() in w for w in target.sock.sent))

    def test_cleanup_after_running_wait_interrupts_before_detach(self):
        target=rsp(packet(b'T02thread:1;')+b'+'+packet(b'OK')+b'+'+packet(b'OK'))
        target.stopped=False
        self.assertTrue(target.cleanup(CALLBACK)['detached'])
        self.assertEqual(target.sock.sent[0],b'\x03')

    def test_cleanup_failure_is_recorded_with_resume_attempt(self):
        target=rsp(b'+'+packet(b'E01')+b'+')
        result=target.cleanup(CALLBACK)
        self.assertFalse(result['detached'])
        self.assertIn('error',result)
        self.assertTrue(result['execution_resume_requested'])
        self.assertIn(b'$c#63',target.sock.sent)

    def fixture(self,path):
        run=path/'run';artifact=path/'artifact'
        run.mkdir();artifact.mkdir()
        kernel=artifact/'kernel'
        segment=struct.pack('<II16s4Q4I',0x19,72,b'__TEXT_EXEC',CALLBACK,0x1000,104,32,7,5,0,0)
        kernel.write_bytes(struct.pack('<8I',0xFEEDFACF,0x100000C,0,2,1,72,0,0)+segment+b'K'*32)
        digest=smp.sha256(kernel)
        (artifact/'kernel.sha256').write_text(digest+'  kernel\n')
        (artifact/'source-commit.txt').write_text('a'*40)
        (artifact/'kernel.syms').write_text(f'{CALLBACK:x} s _lck_ticket_timeout_panic\n{LOCK-0x100:x} S _pset0\n')
        command=dict(argv=['qemu','-M','virt,gic-version=3','-smp','4','-gdb','tcp:127.0.0.1:4444'],
                     inputs=dict(kernel=dict(path=str(kernel),size=kernel.stat().st_size,sha256=digest),
                                 system=dict(size=123,sha256='b'*64)))
        (run/'command.json').write_text(json.dumps(command))
        (run/'progress.json').write_text(json.dumps(dict(status='running',kernel_started_host_seconds=5,
                 system_transfer=dict(bytes=123,sha256='b'*64,readback_samples=3))))
        return run,artifact

    def test_exact_provenance_and_macho_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            run,artifact=self.fixture(Path(tmp))
            result=smp.prepare(run,artifact)
            self.assertEqual(result['port'],4444)
            self.assertEqual(result['expected'],b'K'*32)
            self.assertEqual(result['lock'],LOCK)
            self.assertEqual(result['provenance']['source_commit'],'a'*40)
            (artifact/'kernel.sha256').write_text('0'*64+' kernel')
            with self.assertRaisesRegex(ValueError,'artifact kernel hash'):
                smp.prepare(run,artifact)

    def test_provenance_rejects_remote_stale_and_unfinished(self):
        for kind in ('remote','finished','preload','changed_kernel','wrong_source'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                run,artifact=self.fixture(Path(tmp))
                if kind=='remote':
                    command=smp.bounded_json(run/'command.json');command['argv'][-1]='tcp:192.168.0.230:8022'
                    (run/'command.json').write_text(json.dumps(command))
                elif kind=='finished':(run/'result.json').write_text('{}')
                elif kind=='preload':(run/'progress.json').write_text('{"status":"loading_system"}')
                elif kind=='changed_kernel':(artifact/'kernel').write_bytes(b'changed')
                else:(artifact/'source-commit.txt').write_text('main')
                with self.assertRaises(ValueError):smp.prepare(run,artifact)

    def test_macho_invalid_or_bss_symbol_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run,artifact=self.fixture(Path(tmp));kernel=artifact/'kernel'
            with self.assertRaisesRegex(ValueError,'file-backed'):
                smp.macho_bytes(kernel,CALLBACK+32,1)
            kernel.write_bytes(b'not macho')
            with self.assertRaisesRegex(ValueError,'truncated'):
                smp.macho_bytes(kernel,CALLBACK,32)

    def test_cli_failure_writes_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run,artifact=self.fixture(Path(tmp));out=Path(tmp)/'capture.json'
            with patch.object(sys,'argv',['smp_capture','--run',str(run),'--artifact',str(artifact),'--output',str(out)]),patch.object(smp,'RSP',side_effect=OSError('fixture connection refused')):
                self.assertEqual(smp.main(),1)
            result=json.loads(out.read_text())
            self.assertTrue(result['incomplete'])
            self.assertFalse(result['boot_test_pass'])
            self.assertIn('connection refused',result['error'])


if __name__=='__main__':
    unittest.main()
