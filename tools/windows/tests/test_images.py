import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_images import from_manifest, image, link, regular, directory, lf_copy
from extract_ios import safe_path, host_path
import apfs


class ImageTests(unittest.TestCase):
    def test_boot_configuration_uses_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'source', Path(tmp) / 'target'
            source.write_bytes(b'# fixture\r\nexec /sbin/launchd.ios\r\n')
            lf_copy(source, target)
            self.assertEqual(target.read_bytes(), b'# fixture\nexec /sbin/launchd.ios\n')

    def test_ntfs_unsafe_paths(self):
        for name in ('/root', '../a', 'a/../b', 'C:/a', 'a:stream', 'a\\b', 'CON',
                     'nul.txt', 'file.', 'file ', 'a\nb', 'LPT1.txt'):
            with self.assertRaises(ValueError, msg=name):
                safe_path(name)
        self.assertEqual(safe_path('System/Русский.bundle/file'),
                         ('System', 'Русский.bundle', 'file'))

    def test_virtual_posix_metadata_and_staged_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            files = tmp / 'files'
            staged = files / 'private/var/staged_system_apps/Test.app'
            staged.mkdir(parents=True)
            (staged / 'Test').write_bytes(b'fixture executable' * 1000)
            manifest = tmp / 'manifest'
            manifest.write_text('40755\t0\t0\tApplications\t\n'
                                '40755\t501\t501\tApplications/Test.app\t\n'
                                '100755\t501\t501\tApplications/Test.app/Test\t\n'
                                '120777\t0\t0\talias\t/Applications/Test.app\n', encoding='utf-8')
            tree = from_manifest(files, manifest)
            path = tmp / 'test.apfs'
            image(tree, 'Test', path)
            container = apfs.Container(str(path))
            try:
                volume = container.volumes()[0]
                inode = volume.inode(volume.lookup('/Applications/Test.app/Test')[0])
                self.assertEqual(inode.contents(), (staged / 'Test').read_bytes())
                self.assertEqual((inode.mode, inode.owner, inode.group), (stat.S_IFREG | 0o755, 501, 501))
                alias = volume.inode(volume.lookup('/alias')[0])
                self.assertTrue(alias.is_link)
                self.assertEqual(alias.contents(), b'/Applications/Test.app')
            finally:
                container.close()

    def test_sequential_equals_two_workers(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / 'source'
            source.mkdir()
            for name, data in [('empty', b''), ('inline', b'x' * 200),
                               ('resource', b'abcdefgh' * 40000), ('plain', os.urandom(90000))]:
                (source / name).write_bytes(data)
            hashes = []
            for workers in (1, 2):
                folder = tmp / str(workers)
                folder.mkdir()
                output = folder / 'test.apfs'  # same UUID seed
                subprocess.run([sys.executable, str(root / 'tools/apfs/mkapfs.py'), '-z',
                                '--jobs', str(workers), '-o', str(output), 'Test=' + str(source)],
                               check=True, capture_output=True)
                hashes.append(hashlib.sha256(output.read_bytes()).digest())
            self.assertEqual(*hashes)

    def test_missing_selected_regular_file_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = tmp / 'manifest'
            manifest.write_text('100755\t0\t0\tsbin/launchd\t\n')
            with self.assertRaisesRegex(ValueError, 'missing selected'):
                from_manifest(tmp, manifest)

    def test_ntfs_remap_restores_literal_asterisk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            files = tmp / 'files'
            (files / '__ntfs_names').mkdir(parents=True)
            (files / '__ntfs_names/encoded').write_bytes(b'{"fixture":true}')
            original = 'private/var/staged_system_apps/Test.app/*.example.com.json'
            files.with_name('files.names.json').write_text(json.dumps({original: '__ntfs_names/encoded'}))
            manifest = tmp / 'manifest'
            manifest.write_text('100644\t0\t0\tApplications/Test.app/*.example.com.json\t\n')
            tree = from_manifest(files, manifest)
            output = tmp / 'test.apfs'
            image(tree, 'Test', output)
            container = apfs.Container(str(output))
            try:
                volume = container.volumes()[0]
                inode = volume.inode(volume.lookup('/Applications/Test.app/*.example.com.json')[0])
                self.assertEqual(inode.contents(), b'{"fixture":true}')
            finally:
                container.close()

    def test_deep_framework_path(self):
        with tempfile.TemporaryDirectory(dir=str(host_path(tempfile.gettempdir()))) as tmp:
            tmp = Path(tmp)
            files = tmp / 'files'
            logical = '/'.join(['Frameworks', 'a' * 80, 'b' * 80, 'c' * 80, 'data'])
            source = host_path(files / logical)
            source.parent.mkdir(parents=True)
            source.write_bytes(b'long framework path')
            manifest = tmp / 'manifest'
            manifest.write_text(f'100644\t0\t0\t{logical}\t\n')
            tree = from_manifest(files, manifest)
            node = tree
            for part in logical.split('/'):
                node = node.children[part]
            self.assertEqual(Path(node.source).read_bytes(), b'long framework path')


if __name__ == '__main__':
    unittest.main()
