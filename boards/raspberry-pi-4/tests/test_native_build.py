"""Native build isolation, snapshot cleanup and offline boot contract checks."""

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "scripts" / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


MAKE = module("native_make", "make-images.py")
VERIFY = module("native_verify", "verify-images.py")
QEMU = module("native_qemu", "verify-qemu.py")


class BuildModeTests(unittest.TestCase):
    def test_native_and_explicit_uefi_artifacts_are_separate(self):
        self.addCleanup(MAKE.configure_mode, "native")
        MAKE.configure_mode("uefi")
        legacy = (MAKE.ROOT, MAKE.BUILD, MAKE.ARTIFACTS, MAKE.IMAGE_NAME)
        self.assertEqual(legacy, (PROJECT / "build/rootfs", PROJECT / "build/images",
                                  PROJECT / "artifacts", "rpi4-archlinux-ports-sd-usb.img"))
        MAKE.configure_mode("native")
        self.assertEqual((MAKE.ROOT, MAKE.BUILD, MAKE.ARTIFACTS, MAKE.IMAGE_NAME),
                         (PROJECT / "build/native/rootfs", PROJECT / "build/native/images",
                          PROJECT / "artifacts/native", "rpi4-archlinux-ports-native-sd-usb.img"))
        first, second = MAKE.boot_metadata(), MAKE.boot_metadata()
        for key in ("disk_uuid", "boot_partuuid", "root_partuuid", "root_fs_uuid", "boot_fs_uuid"):
            self.assertNotEqual(first[key], second[key])
        with self.assertRaises(ValueError):
            MAKE.configure_mode("invalid")

    def test_clone_is_separate_and_existing_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for directory in ("scripts", "configs", "commands", "snapshot/usr/lib/modules", "snapshot/etc"):
                (project / directory).mkdir(parents=True)
            for name in ("build-rootfs.sh", "build-paths.sh", "rootfs-container.sh"):
                shutil.copy2(PROJECT / "scripts" / name, project / "scripts" / name)
            (project / "configs/sources.env").write_text("")
            (project / "scripts/fetch-inputs.sh").write_text("#!/bin/bash\nexit 0\n")
            (project / "scripts/fetch-inputs.sh").chmod(0o755)
            source = project / "snapshot/etc/pacman.conf"
            source.write_text("snapshot package configuration\n")
            podman = project / "commands/podman"
            podman.write_text(f"#!{sys.executable}\nimport os,subprocess,sys\n"
                              "if sys.argv[1] == 'unshare':\n"
                              "    sys.exit(subprocess.call(sys.argv[2:]))\n"
                              "assert sys.argv[1] == 'exec'\n"
                              "assert os.environ['RPI4_BOOT_MODE'] == 'native'\n"
                              "assert os.environ['RPI4_REUSE_PACKAGES'] == '1'\n")
            podman.chmod(0o755)
            environment = os.environ | {"RPI4_BOOT_MODE": "native", "RPI4_ROOTFS_SNAPSHOT": str(project / "snapshot"),
                                        "PATH": str(project / "commands") + os.pathsep + os.environ["PATH"]}
            command = ["bash", str(project / "scripts/build-rootfs.sh")]
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            clone = project / "build/native/rootfs/etc/pacman.conf"
            self.assertEqual(clone.read_bytes(), source.read_bytes())
            self.assertNotEqual(clone.stat().st_ino, source.stat().st_ino)
            clone.write_text("changed clone")
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("destination rootfs must not already exist", result.stderr)
            self.assertEqual(source.read_text(), "snapshot package configuration\n")
            self.assertFalse((project / "build/rootfs").exists())
            shutil.rmtree(project / "build/native/rootfs")
            (project / "build/native/usr/lib/modules").mkdir(parents=True)
            (project / "build/native/etc").mkdir()
            (project / "build/native/etc/pacman.conf").write_text("recursive source")
            environment["RPI4_ROOTFS_SNAPSHOT"] = str(project / "build/native")
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Snapshot and destination must be separate", result.stderr)
            self.assertFalse((project / "build/native/rootfs").exists())

    def test_snapshot_cleanup_replaces_identity_and_uses_actual_package_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = project / "build/native/rootfs"
            shutil.copytree(PROJECT / "overlay", project / "overlay")
            shutil.copytree(PROJECT / "configs/native", project / "configs/native")
            for directory in ("etc", "boot", "usr/local/sbin", "var/cache/pacman/pkg", "usr/share/rpi4"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            secrets = ("home/rpi/.ssh/authorized_keys", "root/.ssh/authorized_keys", "etc/ssh/ssh_host_ed25519_key",
                       "etc/pacman.d/gnupg/private-keys-v1.d/secret", "root/.gnupg/secret",
                       "var/lib/rpi4/initialized", "var/lib/rpi4/root-expanded", "var/lib/systemd/random-seed",
                       "var/lib/systemd/credential.secret", "var/lib/systemd/rfkill/snapshot")
            for name in secrets:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("snapshot identity")
            (root / "etc/machine-id").write_text("old machine")
            (root / "etc/hostname").write_text("rpi4-native-test\n")
            (root / "usr/share/rpi4/initramfs-boot-mode").write_text("native\n")
            (project / "build/native/package-manifest.txt").write_text("linux exact-snapshot-version\n")
            (project / "build/package-manifest.txt").write_text("linux stale-uefi-version\n")
            with patch.multiple(MAKE, PROJECT=project, ROOT=root, BOOT_MODE="native"), patch.object(MAKE, "run"):
                MAKE.prepare_tree()
            for name in secrets:
                self.assertFalse((root / name).exists(), name)
            self.assertEqual((root / "etc/machine-id").read_bytes(), b"")
            self.assertEqual((root / "etc/hostname").read_bytes(), b"rpi4\n")
            self.assertEqual((root / "usr/share/rpi4/package-manifest.txt").read_text(), "linux exact-snapshot-version\n")
            self.assertEqual((root / "etc/modules-load.d/rpi4-display.conf").read_bytes(),
                             (PROJECT / "configs/native/modules-load.conf").read_bytes())
            self.assertNotIn(b"simpledrm", (root / "etc/modules-load.d/rpi4-display.conf").read_bytes())


class NativeVerificationTests(unittest.TestCase):
    def fixture(self, change=None):
        root_uuid = "12345678-1234-1234-1234-123456789abc"
        image = bytearray(128)
        struct.pack_into("<QQQ", image, 8, 0, len(image), 0xA)
        image[56:60] = b"ARM\x64"
        dtb = bytearray(40) + b"raspberrypi,4-model-b\0spin-table\0"
        struct.pack_into(">II", dtb, 0, 0xD00DFEED, len(dtb))
        payloads = {"kernel8.img": gzip.compress(image), "bcm2711-rpi-4-b.dtb": bytes(dtb),
                    "initramfs-linux.img": b"initramfs" * 256, "kernel-version": b"7.2.9-test\n"}
        base = (f"root=PARTUUID={root_uuid} rw rootwait rootfstype=ext4 "
                "cma=256M@0-1G module_blacklist=simpledrm console=tty1")
        change = dict(change or {})
        payloads.update({name: data for name, data in change.items() if name != "cmdline.txt"})
        backend = VERIFY.native_backend()
        payload_sums = "".join(f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n"
                               for name in backend["PAYLOAD"])
        release = backend["boot_set_id"](payload_sums, base)
        payloads["cmdline.txt"] = change.get("cmdline.txt", f"{base} rpi4.boot_set={release}\n".encode())
        sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in payloads.items()).encode()
        prefix = "native/" + release
        selector = (f"kernel={prefix}/kernel8.img\ndevice_tree={prefix}/bcm2711-rpi-4-b.dtb\n"
                    f"initramfs {prefix}/initramfs-linux.img followkernel\ncmdline={prefix}/cmdline.txt\n")
        files = {f"/{prefix}/{name}": data for name, data in payloads.items()}
        files[f"/{prefix}/SHA256SUMS"] = sums
        files["/native-current.txt"] = selector.encode()
        files["/native-fallback.txt"] = ("# Fallback kernel 7.2.9-test\n" + selector).encode()
        files["/config.txt"] = (PROJECT / "configs/native/config.txt").read_bytes()
        hashes = {name.lstrip("/"): hashlib.sha256(data).hexdigest() for name, data in files.items()}
        metadata = {"boot_mode": "native", "root_partuuid": root_uuid,
                    "native_selector": files["/native-current.txt"].decode(),
                    "native_fallback_selector": files["/native-fallback.txt"].decode(),
                    "boot_files_sha256": hashes, "firmware": {"files_sha256": {"config.txt": hashes["config.txt"]}}}
        return files, metadata

    def verify(self, files, metadata):
        with patch.object(VERIFY, "fat_file", side_effect=lambda filesystem, name: files[name]):
            return VERIFY.verify_boot(Path("fixture.fat32"), metadata)

    def test_native_raw_kernel_dtb_selector_and_root_are_validated(self):
        files, metadata = self.fixture()
        self.assertEqual(self.verify(files, metadata)["kernel_version"], "7.2.9-test")
        files["/native-current.txt"] += b"kernel=unsafe\n"
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.verify(files, metadata)

    def test_rehashed_invalid_payloads_are_rejected(self):
        for change in ({"kernel8.img": gzip.compress(b"invalid Image")},
                       {"bcm2711-rpi-4-b.dtb": b"wrong board"},
                       {"cmdline.txt": b"root=PARTUUID=wrong rw rootwait\n"}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                self.verify(*self.fixture(change))

    def test_boot_set_name_and_fallback_selector_are_checked(self):
        files, metadata = self.fixture()
        name = metadata["native_selector"].split("native/")[1][:24]
        other = "0" * 24
        cases = {"cmdline names another set": lambda f, m: f.update({
                     f"/native/{name}/cmdline.txt": f[f"/native/{name}/cmdline.txt"].replace(name.encode(), other.encode())}),
                 "fallback points elsewhere": lambda f, m: m.update({
                     "native_fallback_selector": m["native_fallback_selector"].replace(name, other)})}
        for label, mutate in cases.items():
            with self.subTest(label=label):
                files, metadata = self.fixture()
                mutate(files, metadata)
                if label == "fallback points elsewhere":
                    files["/native-fallback.txt"] = metadata["native_fallback_selector"].encode()
                else:
                    metadata["boot_files_sha256"][f"native/{name}/cmdline.txt"] = hashlib.sha256(
                        files[f"/native/{name}/cmdline.txt"]).hexdigest()
                with self.assertRaises(ValueError):
                    self.verify(files, metadata)

    def test_selector_rejects_mixed_sets_traversal_and_repeated_directives(self):
        _, metadata = self.fixture()
        selector = metadata["native_selector"]
        cases = [selector.replace("kernel=native/", "kernel=../native/"),
                 selector + selector.splitlines()[0] + "\n",
                 selector.replace("followkernel", "0x800000"),
                 selector.replace("device_tree=native/", "device_tree=native/" + "a" * 24 + "/")]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                VERIFY.native_fields(text)

    def test_guest_checks_require_correct_firmware_mode_and_keep_security_checks(self):
        for mode, assertion in (("uefi", "[[ -d /sys/firmware/efi ]]"),
                                ("native", "[[ ! -d /sys/firmware/efi ]]")):
            script = QEMU.guest_script("7.2.9-test", "root", "boot", 1024, 900, mode)
            result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(assertion, script)
            for check in ("RPI4_QEMU_GROWTH", "--fingerprint", "NTPSynchronized", "RPI4_QEMU_SSH_LOGIN",
                          "pacman -Qk", "PasswordAuthentication=no"):
                self.assertIn(check, script)


if __name__ == "__main__":
    unittest.main()
