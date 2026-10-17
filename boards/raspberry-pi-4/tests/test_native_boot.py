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
                  'elif [[ $1 == -Q ]]; then\n'
                  ' echo "linux 7.2.9.arch1-1"\n'
                  ' echo "linux-dtbs ${BOOT_TEST_DTBS_PACKAGE:-7.2.9.arch1-1}"\n'
                  'else exit 2; fi\n')
        source = SOURCE.read_text()
        for path in ('/usr/local/sbin/rpi4-check-boot', '/usr/lib/modules',
                     '/usr/share/rpi4', '/etc/rpi4-image.conf', '/run/lock', '/boot'):
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
        selected = re.search(r'^kernel=native/([a-f0-9]{24})/kernel8.img$',
                             self.selector.read_text(), re.M)
        self.assertIsNotNone(selected)
        return self.boot / 'native' / selected[1]

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
        sums = (selected / 'SHA256SUMS').read_bytes()
        self.assertEqual(hashlib.sha256(sums).hexdigest()[:24], selected.name)
        result = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=selected, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_retains_previous_set_and_refuses_corrupt_content(self):
        first = self.successful_update()
        self.kernel.write_bytes(arm64_image(b'kernel two'))
        second = self.successful_update()
        self.assertIn(f'# previous-boot-set {first.name}', self.selector.read_text())
        self.assertEqual(self.successful_update(), second)
        self.assertTrue(first.exists())
        self.kernel.write_bytes(arm64_image(b'kernel three'))
        third = self.successful_update()
        self.assertEqual(set((self.boot / 'native').iterdir()), {second, third})
        before = self.selector.read_bytes()
        (third / 'kernel8.img').write_bytes(b'corrupt')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot set is damaged', result.stderr)
        self.assertEqual(self.selector.read_bytes(), before)
        self.assertIn('not an OS rollback', self.selector.read_text())

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


if __name__ == '__main__':
    unittest.main()
