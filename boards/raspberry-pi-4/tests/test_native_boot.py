"""Native kernel extraction and boot promotion using temporary regular files."""

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_boot as boot_fixture


SOURCE = boot_fixture.OVERLAY / 'usr/local/lib/rpi4/update-native-boot.py'


def load_backend(path=SOURCE):
    spec = importlib.util.spec_from_file_location('rpi4_native_backend', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arm64_image(payload=b'kernel', memory_size=256):
    data = bytearray(64)
    struct.pack_into('<QQQ', data, 8, 0, memory_size, 0xA)
    data[56:60] = b'ARM\x64'
    return bytes(data) + payload


def zboot(image, compression=b'zstd'):
    padded = image + bytes(256 - len(image))
    if compression == b'zstd':
        payload = subprocess.check_output(['zstd', '-q', '-c'], input=padded)
        trailer = struct.pack('<I', len(padded))
    else:
        payload = gzip.compress(padded, mtime=0)
        trailer = b''
    header = bytearray(256)
    header[:8] = b'MZ\0\0zimg'
    struct.pack_into('<II', header, 8, len(header), len(payload))
    header[24:24 + len(compression)] = compression
    header[56:60] = b'\xcd\x23\x82\x81'
    struct.pack_into('<I', header, 60, 64)
    header[64:68] = b'PE\0\0'
    struct.pack_into('<H', header, 68, 0xAA64)
    return bytes(header) + payload + trailer + b'PE alignment padding'


def dtb():
    # Header and identifying data suffice for this backend's format guard.
    # The image verifier additionally compares the complete packaged DTB.
    data = bytearray(40) + b'raspberrypi,4-model-b\0spin-table\0'
    struct.pack_into('>II', data, 0, 0xD00DFEED, len(data))
    return bytes(data)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.backend = load_backend()

    def test_raw_and_gzip_images_pad_bss_before_followkernel_placement(self):
        image = arm64_image()
        expected = image + bytes(256 - len(image))
        self.assertEqual(self.backend.raw_image(image), expected)
        self.assertEqual(self.backend.raw_image(gzip.compress(image)), expected)

    def test_gzip_zboot_extracts_padded_image(self):
        image = arm64_image()
        self.assertEqual(self.backend.raw_image(zboot(image, b'gzip')),
                         image + bytes(256 - len(image)))

    @unittest.skipUnless(shutil.which('zstd'), 'zstd is required by the production backend')
    def test_zstd_zboot_validates_expanded_size_trailer_and_architecture(self):
        image = arm64_image()
        wrapped = zboot(image)
        self.assertEqual(self.backend.raw_image(wrapped), image + bytes(256 - len(image)))
        offset, length = struct.unpack_from('<II', wrapped, 8)
        corrupt = bytearray(wrapped)
        struct.pack_into('<I', corrupt, offset + length, 255)
        with self.assertRaisesRegex(ValueError, 'expanded size'):
            self.backend.raw_image(bytes(corrupt))
        corrupt = bytearray(wrapped)
        struct.pack_into('<H', corrupt, 68, 0x8664)
        with self.assertRaisesRegex(ValueError, 'AArch64'):
            self.backend.raw_image(bytes(corrupt))
        with self.assertRaisesRegex(ValueError, 'trailer'):
            self.backend.raw_image(wrapped[:offset + length])

    def test_gzip_expansion_limit_is_enforced_before_image_validation(self):
        compressed = gzip.compress(arm64_image(b'x' * 4096, memory_size=8192))
        with patch.object(self.backend, 'MAX_IMAGE_BYTES', 1024):
            with self.assertRaisesRegex(ValueError, 'expanded size exceeds'):
                self.backend.raw_image(compressed)

    def test_rejects_unsupported_arm64_layout_and_invalid_dtb(self):
        image = arm64_image()
        for offset, value in ((8, 0x80000), (16, 63), (16, 64), (24, 0xB), (24, 0xC)):
            with self.subTest(offset=offset, value=value):
                corrupt = bytearray(image)
                struct.pack_into('<Q', corrupt, offset, value)
                with self.assertRaises(ValueError):
                    self.backend.raw_image(bytes(corrupt))
        self.backend.validate_dtb(dtb())
        for invalid in (b'', dtb()[:-1], dtb().replace(b'spin-table', b'psci------')):
            with self.assertRaises(ValueError):
                self.backend.validate_dtb(invalid)


class NativeBootTests(unittest.TestCase):
    mock = boot_fixture.BootTests.mock
    run_script = boot_fixture.BootTests.run_script

    def setUp(self):
        boot_fixture.BootTests.setUp(self)
        self.config.write_text(self.config.read_text() + 'RPI4_BOOT_MODE=native\n')
        self.kernel.write_bytes(arm64_image())
        self.dtb.write_bytes(dtb())
        self.selector = self.boot / 'native-current.txt'
        (self.boot / 'config.txt').write_text(
            '[all]\narm_64bit=1\nenable_gic=1\nkernel_address=0x200000\n'
            'start_file=start4.elf\nfixup_file=fixup4.dat\ninclude native-current.txt\n')
        (self.boot / 'overlays').mkdir()
        (self.boot / 'overlays/rpi4-usb-c-data-disable.dtbo').write_bytes(b'USB-C overlay')
        hashes = {}
        for name in ('start4.elf', 'fixup4.dat'):
            data = f'fixture {name}'.encode()
            (self.boot / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        (self.marker.parent / 'runtime-firmware.json').write_text(json.dumps({'sha256': hashes}))
        self.mock('pacman',
                  'if [[ $1 == -Qql ]]; then\n'
                  ' printf "%s\\n" "$BOOT_TEST_KERNEL"\n'
                  ' [[ ${BOOT_TEST_DUPLICATE_KERNEL:-0} != 1 ]] || printf "%s\\n" "$BOOT_TEST_KERNEL"\n'
                  'elif [[ $1 == -Qqo && $3 == *.dtb ]]; then echo linux-dtbs\n'
                  'elif [[ $1 == -Qqo && $3 == "${BOOT_TEST_OWNED_MODULES:-}" ]]; then echo linux\n'
                  'elif [[ $1 == -Qqo && $3 == */vmlinuz && -e $3 ]]; then echo linux\n'
                  'elif [[ $1 == -Q ]]; then\n'
                  ' echo "linux 7.2.9.arch1-1"\n'
                  ' echo "linux-dtbs ${BOOT_TEST_DTBS_PACKAGE:-7.2.9.arch1-1}"\n'
                  'else exit 2; fi\n')
        self.cmdline = self.root / 'proc/cmdline'
        self.cmdline.parent.mkdir()
        self.cmdline.write_text('console=tty1\n')
        self.osrelease = self.root / 'proc/sys/kernel/osrelease'
        self.osrelease.parent.mkdir(parents=True)
        self.osrelease.write_text(boot_fixture.KVER + '\n')
        self.modules = self.kernel.parent.parent
        (self.modules / boot_fixture.KVER / 'modules.order').write_text('fixture modules\n')
        self.fallback_selector = self.boot / 'native-fallback.txt'
        source = SOURCE.read_text()
        for path in ('/usr/local/sbin/rpi4-check-boot', '/usr/lib/modules', '/proc/cmdline',
                     '/proc/sys/kernel/osrelease', '/usr/share/rpi4', '/etc/rpi4-image.conf',
                     '/run/lock', '/boot'):
            source = source.replace(path, str(self.root / path.lstrip('/')))
        source = source.replace('os.geteuid() != 0', 'False')
        self.backend = self.root / 'usr/local/lib/rpi4/update-native-boot.py'
        self.backend.write_text(source)
        script = self.scripts['rpi4-update-boot']
        script.write_text(script.read_text().replace('/usr/local/lib/rpi4/update-native-boot.py',
                                                    str(self.backend))
                          .replace('exec python ', f'exec {sys.executable} '))

    def successful_update(self, **environment):
        result = self.run_script(**environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.selected(self.selector)

    def selected(self, selector):
        match = re.search(r'^kernel=native/([a-f0-9]{24})/kernel8.img$', selector.read_text(), re.M)
        self.assertIsNotNone(match)
        return self.boot / 'native' / match[1]

    def boot_from(self, boot_set):
        self.cmdline.write_text('coherent_pool=1M ' + (boot_set / 'cmdline.txt').read_text())

    def select_fallback_in_config(self):
        config = self.boot / 'config.txt'
        config.write_text(config.read_text().replace('include native-current.txt',
                                                     'include native-fallback.txt'))

    def test_native_dispatch_selects_consistent_set_without_efi_files(self):
        selected = self.successful_update()
        self.assertEqual(gzip.decompress((selected / 'kernel8.img').read_bytes()),
                         self.kernel.read_bytes() + bytes(256 - self.kernel.stat().st_size))
        self.assertEqual((selected / 'bcm2711-rpi-4-b.dtb').read_bytes(), self.dtb.read_bytes())
        self.assertEqual((selected / 'initramfs-linux.img').read_bytes(), b'initramfs one')
        self.assertEqual((self.boot / 'vmlinuz-linux').read_bytes(), self.kernel.read_bytes())
        self.assertFalse(self.fallback.exists())
        self.assertFalse(self.entry.exists())
        selector = self.selector.read_text()
        for name in ('bcm2711-rpi-4-b.dtb', 'initramfs-linux.img', 'cmdline.txt'):
            self.assertIn(f'native/{selected.name}/{name}', selector)
        self.assertIn('followkernel', selector)
        self.assertTrue(all(len(line) <= 80 for line in selector.splitlines()
                            if not line.startswith('#')))
        command_line = (selected / 'cmdline.txt').read_text()
        self.assertIn(f'root=PARTUUID={boot_fixture.PARTUUID}', command_line)
        self.assertIn('module_blacklist=simpledrm', command_line)
        self.assertIn('rfkill.default_state=0', command_line)
        for unexpected in ('acpi=', 'psci_checker', 'snd_bcm2835', 'vc4,v3d'):
            self.assertNotIn(unexpected, command_line)
        base, _, option = command_line.strip().rpartition(' ')
        self.assertEqual(option, f'rpi4.boot_set={selected.name}')
        sums = (selected / 'SHA256SUMS').read_text().splitlines()
        payload = ''.join(line + '\n' for line in sums if not line.endswith('cmdline.txt'))
        self.assertEqual(load_backend(self.backend).boot_set_id(payload, base), selected.name)
        result = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=selected, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.selected(self.fallback_selector), selected)
        self.assertIn('No older boot set', self.run_script().stdout)

    def test_runtime_rebuild_failure_preserves_selection_and_packaged_preset(self):
        selected = self.successful_update()
        before = self.selector.read_bytes()
        preset = (self.boot / 'vmlinuz-linux').read_bytes()
        self.kernel.write_bytes(arm64_image(b'new kernel'))
        result = self.run_script(RPI4_IMAGE_BUILD='0', BOOT_TEST_MKINITCPIO_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.selector.read_bytes(), before)
        self.assertEqual((self.boot / 'vmlinuz-linux').read_bytes(), preset)
        self.assertEqual(list((self.boot / 'native').iterdir()), [selected])
        new = self.successful_update(RPI4_IMAGE_BUILD='0')
        self.assertEqual((new / 'initramfs-linux.img').read_bytes(), b'initramfs rebuilt')

    def test_direct_backend_requires_configuration_outside_explicit_bootstrap(self):
        self.config.unlink()
        before = {path.relative_to(self.boot): path.read_bytes()
                  for path in self.boot.rglob('*') if path.is_file()}
        for mode in (None, '0', '', 'true', '1'):
            with self.subTest(build_flag=mode):
                environment = self.environment.copy()
                environment.pop('RPI4_IMAGE_BUILD')
                if mode is not None:
                    environment['RPI4_IMAGE_BUILD'] = mode
                result = subprocess.run([sys.executable, str(self.backend)], env=environment,
                                        capture_output=True, text=True, timeout=10)
                if mode == '1':
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Image configuration is missing', result.stderr)
        self.assertFalse((self.root / 'run/lock').exists())
        after = {path.relative_to(self.boot): path.read_bytes()
                 for path in self.boot.rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_invalid_inputs_never_select_new_payloads(self):
        self.successful_update()
        before = self.selector.read_bytes()
        for environment in ({'BOOT_TEST_DUPLICATE_KERNEL': '1'},
                            {'BOOT_TEST_DTBS_PACKAGE': 'different-version'},
                            {'RPI4_IMAGE_BUILD': '0', 'BOOT_TEST_MOUNT_EXIT': '1'}):
            with self.subTest(environment=environment):
                self.assertNotEqual(self.run_script(**environment).returncode, 0)
                self.assertEqual(self.selector.read_bytes(), before)
        self.marker.write_text('different-kernel\n')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('initramfs does not match', result.stderr)
        self.marker.write_text(boot_fixture.KVER + '\n')
        (self.boot / 'fixup4.dat').write_bytes(b'mismatched pair')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('firmware mismatch', result.stderr)
        self.assertEqual(self.selector.read_bytes(), before)

    def test_wrong_mode_and_firmware_config_fail_before_creating_boot_set(self):
        self.config.write_text(self.config.read_text().replace('RPI4_BOOT_MODE=native', 'RPI4_BOOT_MODE=uefi'))
        result = subprocess.run([sys.executable, str(self.backend)], env=self.environment,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires RPI4_BOOT_MODE=native', result.stderr)
        self.assertFalse(self.selector.exists())
        self.config.write_text(self.config.read_text().replace('RPI4_BOOT_MODE=uefi', 'RPI4_BOOT_MODE=native'))
        (self.boot / 'config.txt').write_text('kernel=RPI_EFI.fd\n')
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse(self.selector.exists())

    def test_selector_rename_failure_keeps_old_selection_and_payload(self):
        old = self.successful_update()
        before = self.selector.read_bytes()
        self.kernel.write_bytes(arm64_image(b'upgrade'))
        backend = load_backend(self.backend)
        replace = Path.replace

        def fail_selector(path, target):
            if target == self.selector:
                raise OSError('simulated selector rename failure')
            return replace(path, target)

        with patch.dict(os.environ, self.environment), patch.object(Path, 'replace', fail_selector):
            with self.assertRaisesRegex(OSError, 'selector rename'):
                backend.main()
        self.assertEqual(self.selector.read_bytes(), before)
        self.assertTrue(old.is_dir())
        self.assertFalse(list(self.boot.glob('.native-current.txt-*')))

    def test_running_set_becomes_fallback_when_selection_moves(self):
        first = self.successful_update()
        self.boot_from(first)
        self.assertEqual(self.successful_update(), first)
        self.assertEqual(self.selected(self.fallback_selector), first)
        self.kernel.write_bytes(arm64_image(b'kernel two'))
        second = self.successful_update()
        self.assertEqual(self.selected(self.fallback_selector), first)
        self.kernel.write_bytes(arm64_image(b'kernel three'))
        third = self.successful_update()
        self.assertEqual(self.selected(self.fallback_selector), first)
        self.assertEqual(set((self.boot / 'native').iterdir()), {first, third})
        self.assertFalse(second.exists())
        self.boot_from(third)
        self.assertEqual(self.successful_update(), third)
        self.assertEqual(self.selected(self.fallback_selector), first)
        self.kernel.write_bytes(arm64_image(b'kernel four'))
        fourth = self.successful_update()
        self.assertEqual(self.selected(self.fallback_selector), third)
        self.assertEqual(set((self.boot / 'native').iterdir()), {third, fourth})

    def test_fallback_needs_an_intact_set_and_the_running_kernels_modules(self):
        first = self.successful_update()
        self.boot_from(first)
        modules = self.modules / boot_fixture.KVER / 'modules.order'
        modules.unlink()
        self.kernel.write_bytes(arm64_image(b'kernel two'))
        second = self.successful_update()
        self.assertEqual(self.selected(self.fallback_selector), second)
        self.assertFalse(first.exists())
        modules.write_text('restored\n')
        self.kernel.write_bytes(arm64_image(b'kernel three'))
        third = self.successful_update()
        self.fallback_selector.unlink()
        self.boot_from(second)
        backend = load_backend(self.backend)
        self.enterContext(patch.dict(os.environ, self.environment))
        native = self.boot / 'native'
        self.assertEqual(backend.choose_fallback(native, third.name, 'new'), (second.name, boot_fixture.KVER))
        self.osrelease.write_text('7.2.10-arch1-1\n')
        self.assertEqual(backend.choose_fallback(native, third.name, 'new'), (third.name, 'new'))
        self.osrelease.write_text(boot_fixture.KVER + '\n')
        (second / 'initramfs-linux.img').write_bytes(b'damaged')
        self.assertEqual(backend.choose_fallback(native, third.name, 'new'), (third.name, 'new'))
        self.cmdline.write_text(f'rpi4.boot_set={second.name} rpi4.boot_set={third.name}\n')
        self.assertIsNone(backend.booted_set())

    def test_booting_the_fallback_keeps_it_selected_through_updates(self):
        first = self.successful_update()
        self.boot_from(first)
        self.kernel.write_bytes(arm64_image(b'broken kernel'))
        broken = self.successful_update()
        self.select_fallback_in_config()
        self.boot_from(first)
        before = self.fallback_selector.read_bytes()
        self.kernel.write_bytes(arm64_image(b'fixed kernel'))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fixed = self.selected(self.selector)
        self.assertNotIn(fixed, (first, broken))
        self.assertEqual(self.fallback_selector.read_bytes(), before)
        self.assertIn('config.txt boots the fallback', result.stdout)
        self.assertEqual(set((self.boot / 'native').iterdir()), {first, fixed})
        before = {path.name: path.read_bytes() for path in (self.selector, self.fallback_selector)}
        modules = self.modules / boot_fixture.KVER / 'modules.order'
        for damage in ('modules', 'payload'):
            with self.subTest(damage=damage):
                if damage == 'modules':
                    modules.unlink()
                else:
                    modules.write_text('restored\n')
                    (first / 'kernel8.img').write_bytes(b'damaged fallback')
                self.kernel.write_bytes(arm64_image(f'another kernel {damage}'.encode()))
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('fallback boot set selected in config.txt is missing, damaged',
                              result.stderr)
                self.assertEqual({path.name: path.read_bytes()
                                  for path in (self.selector, self.fallback_selector)}, before)

    def test_kept_tree_counts_only_with_its_marker_once_the_package_is_gone(self):
        backend = load_backend(self.backend)
        self.enterContext(patch.dict(os.environ, self.environment))
        tree = self.modules / '7.2.8-arch1-1'
        (tree / 'kernel').mkdir(parents=True)
        (tree / 'modules.order').write_text('kept')
        self.assertFalse(backend.modules_present('7.2.8-arch1-1'))
        (tree / '.rpi4-kept').write_text('')
        self.assertTrue(backend.modules_present('7.2.8-arch1-1'))
        (tree / 'modules.order').unlink()
        self.assertFalse(backend.modules_present('7.2.8-arch1-1'))
        self.assertTrue(backend.modules_present(boot_fixture.KVER))

    def test_returning_to_current_without_reboot_keeps_the_booted_fallback(self):
        first = self.successful_update()
        self.boot_from(first)
        self.kernel.write_bytes(arm64_image(b'broken kernel'))
        broken = self.successful_update()
        self.select_fallback_in_config()
        config = self.boot / 'config.txt'
        config.write_text(config.read_text().replace('include native-fallback.txt',
                                                     'include native-current.txt'))
        self.assertEqual(self.successful_update(), broken)
        self.assertEqual(self.selected(self.fallback_selector), first)

    def old_format_set(self, template):
        base = (template / 'cmdline.txt').read_text().rpartition(' ')[0] + '\n'
        data = {name: (template / name).read_bytes() for name in
                ('kernel8.img', 'bcm2711-rpi-4-b.dtb', 'initramfs-linux.img', 'kernel-version')}
        data['cmdline.txt'] = base.encode()
        sums = ''.join(f'{hashlib.sha256(value).hexdigest()}  {name}\n' for name, value in data.items())
        old = self.boot / 'native' / hashlib.sha256(sums.encode()).hexdigest()[:24]
        old.mkdir()
        for name, value in data.items():
            (old / name).write_bytes(value)
        (old / 'SHA256SUMS').write_text(sums)
        self.cmdline.write_text('coherent_pool=1M ' + base)
        return old

    def test_migration_keeps_the_copied_selector_as_fallback(self):
        for copy_selector in (True, False):
            with self.subTest(copy_selector=copy_selector):
                self.kernel.write_bytes(arm64_image(f'migration {copy_selector}'.encode()))
                current = self.successful_update()
                old = self.old_format_set(current)
                shutil.rmtree(current)
                self.fallback_selector.unlink()
                self.selector.write_text(self.selector.read_text().replace(current.name, old.name))
                if copy_selector:
                    shutil.copyfile(self.selector, self.fallback_selector)
                self.assertEqual(self.successful_update(), current)
                expected = old if copy_selector else current
                self.assertEqual(self.selected(self.fallback_selector), expected)
                self.assertEqual(old.exists(), copy_selector)
                if copy_selector:
                    self.boot_from(current)
                    self.kernel.write_bytes(arm64_image(b'after migration'))
                    self.successful_update()
                    self.assertEqual(self.selected(self.fallback_selector), current)
                    self.assertFalse(old.exists())

    def test_config_must_include_exactly_one_selector(self):
        config = self.boot / 'config.txt'
        original = config.read_text()
        for text in (original.replace('include native-current.txt\n', ''),
                     original + 'include native-fallback.txt\n',
                     original + 'include native-current.txt\n'):
            with self.subTest(text=text):
                config.write_text(text)
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('expected native boot configuration', result.stderr)
                self.assertFalse(self.selector.exists())

    def test_prunes_only_unowned_module_trees_it_kept(self):
        first = self.successful_update()
        self.boot_from(first)
        trees = {}
        for name, marked in (('7.2.7-arch1-1', True), ('7.2.8-arch1-1', True),
                             ('6.18.57-1-rpi', False), ('7.2.6-arch1-1', True)):
            tree = self.modules / name
            (tree / 'kernel').mkdir(parents=True)
            (tree / 'modules.dep').write_text(name)
            if marked:
                (tree / '.rpi4-kept').write_text('')
            trees[name] = tree
        link = self.modules / '7.2.5-arch1-1'
        link.symlink_to(trees['7.2.7-arch1-1'], target_is_directory=True)
        self.osrelease.write_text('7.2.8-arch1-1\n')
        self.kernel.write_bytes(arm64_image(b'kernel two'))
        self.successful_update(BOOT_TEST_OWNED_MODULES=str(trees['7.2.6-arch1-1']))
        self.assertFalse(trees['7.2.7-arch1-1'].exists())
        for name in ('7.2.8-arch1-1', '6.18.57-1-rpi', '7.2.6-arch1-1', boot_fixture.KVER):
            self.assertTrue((self.modules / name).is_dir(), name)
        self.assertTrue(link.is_symlink())

    def test_refuses_corrupt_existing_set(self):
        selected = self.successful_update()
        before = self.selector.read_bytes()
        (selected / 'kernel8.img').write_bytes(b'corrupt')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot set is damaged', result.stderr)
        self.assertEqual(self.selector.read_bytes(), before)

    def test_staging_cleanup_does_not_follow_links_or_remove_operator_files(self):
        selected = self.successful_update()
        native = self.boot / 'native'
        stale = native / '.staging-a1234567'
        stale.mkdir()
        (stale / 'unfinished').write_bytes(b'interrupted')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'sentinel').write_text('keep')
        link = native / '.staging-b1234567'
        link.symlink_to(outside, target_is_directory=True)
        note = native / '.operator-notes'
        note.mkdir()
        self.assertEqual(self.successful_update(), selected)
        self.assertFalse(stale.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / 'sentinel').exists())
        self.assertTrue(note.is_dir())



class KeepRunningModulesTests(unittest.TestCase):
    SCRIPT = boot_fixture.OVERLAY / 'usr/local/lib/rpi4/keep-running-modules'
    HOOKS = boot_fixture.OVERLAY / 'etc/pacman.d/hooks'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rpi4-modules-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.modules = self.root / 'usr/lib/modules'
        self.tree = self.modules / boot_fixture.KVER
        (self.tree / 'kernel/net').mkdir(parents=True)
        self.files = {'kernel/net/brcmfmac.ko.zst': 'driver', 'modules.order': 'order',
                      'vmlinuz': 'kernel', 'modules.dep': 'generated by depmod'}
        for name, text in self.files.items():
            (self.tree / name).write_text(text)
        self.inodes = {name: (self.tree / name).stat().st_ino for name in self.files}
        self.config = self.root / 'etc/rpi4-image.conf'
        self.config.parent.mkdir()
        self.config.write_text('RPI4_BOOT_MODE=native\n')
        osrelease = self.root / 'proc/sys/kernel/osrelease'
        osrelease.parent.mkdir(parents=True)
        osrelease.write_text(boot_fixture.KVER + '\n')
        commands = self.root / 'bin'
        commands.mkdir()
        (commands / 'pacman').write_text('#!/usr/bin/env bash\n'
                                         '[[ $1 == -Qqo && $3 == "${TEST_OWNED:-}" ]] && echo linux\n')
        (commands / 'pacman').chmod(0o755)
        source = self.SCRIPT.read_text()
        for path in ('/usr/lib/modules', '/etc/rpi4-image.conf', '/proc/sys/kernel/osrelease'):
            source = source.replace(path, str(self.root / path.lstrip('/')))
        self.script = self.root / 'keep-running-modules'
        self.script.write_text(source)
        self.environment = {**os.environ, 'PATH': f'{commands}:/usr/bin:/bin'}

    def run_script(self, *arguments, **environment):
        return subprocess.run(['bash', str(self.script), *arguments], text=True, capture_output=True,
                              env={**self.environment, **environment})

    def remove_package_files(self):
        shutil.rmtree(self.tree / 'kernel')
        (self.tree / 'modules.order').unlink()
        (self.tree / 'vmlinuz').unlink()

    def test_restores_hard_links_after_the_package_removed_its_files(self):
        self.assertEqual(self.run_script('save').returncode, 0)
        self.remove_package_files()
        (self.tree / 'modules.dep').unlink()
        (self.tree / 'modules.dep').write_text('regenerated afterwards')
        result = self.run_script('restore')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, inode in self.inodes.items():
            if name != 'modules.dep':
                self.assertEqual((self.tree / name).stat().st_ino, inode, name)
        self.assertEqual((self.tree / 'modules.dep').read_text(), 'regenerated afterwards')
        self.assertTrue((self.tree / '.rpi4-kept').is_file())
        self.assertFalse((self.modules / '.rpi4-running-saved').exists())

    def test_restore_recreates_a_removed_tree(self):
        self.run_script('save')
        shutil.rmtree(self.tree)
        self.assertEqual(self.run_script('restore').returncode, 0)
        self.assertEqual({name: (self.tree / name).read_text() for name in self.files}, self.files)

    def test_installed_kernel_package_is_left_alone(self):
        self.run_script('save')
        self.assertEqual(self.run_script('restore', TEST_OWNED=str(self.tree / 'vmlinuz')).returncode, 0)
        self.assertFalse((self.tree / '.rpi4-kept').exists())
        self.assertFalse((self.modules / '.rpi4-running-saved').exists())

    def test_directory_shared_with_dtbs_does_not_block_restore(self):
        self.run_script('save')
        self.remove_package_files()
        self.assertEqual(self.run_script('restore', TEST_OWNED=str(self.tree)).returncode, 0)
        self.assertEqual({name: (self.tree / name).read_text() for name in self.files}, self.files)
        self.assertTrue((self.tree / '.rpi4-kept').is_file())

    def test_interrupted_transaction_keeps_its_copy_for_the_retry(self):
        self.run_script('save')
        self.remove_package_files()
        self.assertEqual(self.run_script('save').returncode, 0)
        self.assertEqual(self.run_script('restore').returncode, 0)
        for name, inode in self.inodes.items():
            self.assertEqual((self.tree / name).stat().st_ino, inode, name)

    def test_marker_appears_only_after_a_complete_restore(self):
        (self.tree / '.rpi4-kept').write_text('')
        self.assertEqual(self.run_script('save').returncode, 0)
        self.assertFalse((self.modules / '.rpi4-running-saved' / boot_fixture.KVER / '.rpi4-kept').exists())
        self.remove_package_files()
        locked = self.tree / 'kernel'
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        self.assertNotEqual(self.run_script('restore').returncode, 0)
        self.assertFalse((self.tree / '.rpi4-kept').exists())
        self.assertTrue((self.modules / '.rpi4-running-saved').is_dir())
        locked.chmod(0o755)
        self.assertEqual(self.run_script('restore').returncode, 0)
        self.assertTrue((self.tree / '.rpi4-kept').is_file())
        self.assertEqual({name: (self.tree / name).read_text() for name in self.files}, self.files)

    def test_installed_package_clears_an_obsolete_marker(self):
        (self.tree / '.rpi4-kept').write_text('')
        self.run_script('save')
        self.assertEqual(self.run_script('restore', TEST_OWNED=str(self.tree / 'vmlinuz')).returncode, 0)
        self.assertFalse((self.tree / '.rpi4-kept').exists())

    def test_does_nothing_without_proc(self):
        (self.root / 'proc/sys/kernel/osrelease').unlink()
        for action in ('save', 'restore'):
            result = self.run_script(action)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.modules / '.rpi4-running-saved').exists())

    def test_does_nothing_outside_native_boot_or_without_a_running_tree(self):
        for config in ('RPI4_BOOT_MODE=uefi\n', None):
            with self.subTest(config=config):
                if config is None:
                    self.config.unlink()
                else:
                    self.config.write_text(config)
                self.assertEqual(self.run_script('save').returncode, 0)
                self.assertFalse((self.modules / '.rpi4-running-saved').exists())
        self.config.write_text('RPI4_BOOT_MODE=native\n')
        shutil.rmtree(self.tree)
        self.assertEqual(self.run_script('save').returncode, 0)
        self.assertFalse((self.modules / '.rpi4-running-saved').exists())
        self.assertEqual(self.run_script('wrong').returncode, 2)

    def test_marker_and_hooks_match_the_boot_updater(self):
        self.assertEqual(load_backend().KEPT_MODULES, '.rpi4-kept')
        self.assertIn('/.rpi4-kept"', self.SCRIPT.read_text())
        hooks = sorted(path.name for path in self.HOOKS.iterdir())
        for action, when in (('save', 'PreTransaction'), ('restore', 'PostTransaction')):
            name = f'10-rpi4-{action}-running-modules.hook'
            self.assertLess(hooks.index(name), hooks.index('95-rpi4-boot.hook'))
            self.assertLess(name, '60-depmod.hook')
            text = (self.HOOKS / name).read_text()
            self.assertIn('Target = usr/lib/modules/*/vmlinuz', text)
            self.assertIn(f'When = {when}', text)
            self.assertIn(f'Exec = /usr/bin/bash /usr/local/lib/rpi4/keep-running-modules {action}', text)
            self.assertEqual('AbortOnFail' in text, action == 'save')


if __name__ == '__main__':
    unittest.main()
