"""Exercise boot promotion and mount guards using only temporary regular files.

The production scripts keep their fixed privileged paths. Tests relocate those
paths and mock package/mount/initramfs commands, without chroots or host mounts.
"""

import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
OVERLAY = PROJECT / "overlay"
KVER = "7.2.9-arch1-1"
PARTUUID = "06f2c61d-ef9d-480d-8cdf-39e14d88d8bd"


def efi_image(payload: bytes, machine: int = 0xAA64) -> bytes:
    """Small PE header fixture; this is intentionally not an executable kernel."""
    data = bytearray(64 + 94)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, machine)
    struct.pack_into("<H", data, 84, 70)
    struct.pack_into("<H", data, 88, 0x20B)
    struct.pack_into("<H", data, 156, 10)
    return bytes(data) + payload


class BootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rpi4-boot-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.boot = self.root / "boot"
        self.boot.mkdir()
        self.config = self.root / "etc/rpi4-image.conf"
        self.config.parent.mkdir()
        self.config.write_text(
            f"ROOT_PARTUUID={PARTUUID}\nBOOT_FS_UUID=4A3B-2C1D\nIMAGE_FLAVOUR=sd-usb\n"
        )
        self.kernel = self.root / f"usr/lib/modules/{KVER}/vmlinuz"
        self.kernel.parent.mkdir(parents=True)
        self.kernel.write_bytes(efi_image(b"kernel one"))
        self.dtb = self.kernel.parent / "dtb/broadcom/bcm2711-rpi-4-b.dtb"
        self.dtb.parent.mkdir(parents=True)
        self.dtb.write_bytes(b"\xd0\x0d\xfe\xedmatching-test-dtb")
        self.bootloader = self.root / "usr/lib/systemd/boot/efi/systemd-bootaa64.efi"
        self.bootloader.parent.mkdir(parents=True)
        self.bootloader.write_bytes(efi_image(b"systemd boot"))
        (self.boot / "initramfs-linux.img").write_bytes(b"initramfs one")
        self.marker = self.root / "usr/share/rpi4/initramfs-kernel"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(KVER + "\n")
        self.entry = self.boot / "loader/entries/rpi4.conf"
        self.fallback = self.boot / "EFI/BOOT/BOOTAA64.EFI"
        self.vendor = self.boot / "EFI/systemd/systemd-bootaa64.efi"

        validator = self.root / "usr/local/lib/rpi4/validate-efi.py"
        validator.parent.mkdir(parents=True)
        shutil.copyfile(OVERLAY / "usr/local/lib/rpi4/validate-efi.py", validator)
        self.scripts = {}
        for name in ("rpi4-check-boot", "rpi4-update-boot"):
            source = (OVERLAY / "usr/local/sbin" / name).read_text()
            for path in (
                "/usr/local/sbin/rpi4-check-boot",
                "/usr/local/lib/rpi4/validate-efi.py",
                "/usr/share/rpi4",
                "/usr/lib/modules",
                "/usr/lib/systemd/boot",
                "/etc/rpi4-image.conf",
                "/run/lock",
            ):
                source = source.replace(path, str(self.root / path.lstrip("/")))
            source = re.sub(r"(?<![\w/.-])/boot\b", str(self.boot), source)
            source = source.replace("[[ $EUID -eq 0 ]]", "[[ 0 -eq 0 ]]")
            source = source.replace("\npython ", f"\n{sys.executable} ")
            script = self.root / "usr/local/sbin" / name
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(source)
            script.chmod(0o755)
            self.scripts[name] = script

        self.mock(
            "pacman",
            'if [[ $1 == -Qql ]]; then\n'
            '    printf "%s\\n" "$BOOT_TEST_KERNEL"\n'
            '    [[ ${BOOT_TEST_DUPLICATE_KERNEL:-0} != 1 ]] || printf "%s\\n" "$BOOT_TEST_KERNEL"\n'
            'elif [[ $1 == -Qqo && $3 == *.dtb ]]; then\n'
            '    echo linux-dtbs\n'
            'elif [[ $1 == -Qqo && $3 == *.efi ]]; then\n'
            '    echo systemd\n'
            'else exit 2; fi\n',
        )
        self.mock("mountpoint", 'exit "${BOOT_TEST_MOUNT_EXIT:-0}"\n')
        self.mock(
            "findmnt",
            'case "$3" in\n'
            '    UUID) echo "${BOOT_TEST_UUID:-4a3b-2c1d}" ;;\n'
            '    FSTYPE) echo "${BOOT_TEST_FSTYPE:-vfat}" ;;\n'
            '    OPTIONS) echo "${BOOT_TEST_OPTIONS:-rw,nosuid,nodev,noexec}" ;;\n'
            '    *) exit 2 ;;\nesac\n',
        )
        self.mock(
            "mkinitcpio",
            '[[ $1 == -k && $2 == "${BOOT_TEST_KVER}" && $3 == -g ]] || exit 2\n'
            'printf "%s" "${BOOT_TEST_INITRAMFS:-initramfs rebuilt}" > "$4"\n'
            'exit "${BOOT_TEST_MKINITCPIO_EXIT:-0}"\n',
        )
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "BOOT_TEST_KERNEL": str(self.kernel),
            "BOOT_TEST_KVER": KVER,
            "RPI4_IMAGE_BUILD": "1",
        }

    def mock(self, name, body):
        script = self.bin / name
        script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        script.chmod(0o755)

    def run_script(self, name="rpi4-update-boot", **environment):
        return subprocess.run(
            [str(self.scripts[name])],
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
        )

    def successful_update(self, **environment):
        result = self.run_script(**environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = re.search(r"^linux (/rpi4/[^\n]+)$", self.entry.read_text(), re.M)[1]
        return (self.boot / path.lstrip("/")).parent

    def test_unconfigured_build_hooks_skip_without_writes(self):
        self.config.unlink()
        for name in self.scripts:
            result = self.run_script(name, RPI4_IMAGE_BUILD="1", BOOT_TEST_MOUNT_EXIT="1")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.boot / "rpi4").exists())
        self.assertFalse((self.root / "run/lock").exists())

    def test_unconfigured_runtime_hooks_fail_without_writes(self):
        self.config.unlink()
        self.environment.pop("RPI4_IMAGE_BUILD")
        before = {path.relative_to(self.boot): path.read_bytes()
                  for path in self.boot.rglob("*") if path.is_file()}
        for mode in (None, "0", "", "true"):
            for name in self.scripts:
                with self.subTest(script=name, build_flag=mode):
                    environment = {} if mode is None else {"RPI4_IMAGE_BUILD": mode}
                    result = self.run_script(name, BOOT_TEST_MOUNT_EXIT="1", **environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Image configuration is missing", result.stderr)
        self.assertFalse((self.root / "run/lock").exists())
        after = {path.relative_to(self.boot): path.read_bytes()
                 for path in self.boot.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_font_package_hooks_pair_preflight_with_selected_initramfs_refresh(self):
        hooks = []
        for name in ("05-rpi4-boot-mounted.hook", "95-rpi4-boot.hook"):
            parts = re.split(r"(?m)^\[([^\]]+)\]\s*$",
                             (OVERLAY / "etc/pacman.d/hooks" / name).read_text())
            sections = []
            for section, body in zip(parts[1::2], parts[2::2]):
                values = {}
                for line in body.splitlines():
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    values.setdefault(key.strip(), []).append(value.strip())
                sections.append((section, values))
            hooks.append(sections)
        self.assertEqual(hooks[0][:-1], hooks[1][:-1])
        pre, post = (sections[-1][1] for sections in hooks)
        self.assertEqual(pre["When"], ["PreTransaction"])
        self.assertIn("AbortOnFail", pre)
        self.assertEqual(pre["Exec"], ["/usr/local/sbin/rpi4-check-boot"])
        self.assertEqual(post["When"], ["PostTransaction"])
        self.assertEqual(post["Exec"], ["/usr/local/sbin/rpi4-update-boot"])
        for package in ("kbd", "fbset", "terminus-font"):
            matching = [values for section, values in hooks[0] if section == "Trigger"
                        and values["Type"] == ["Package"] and "Upgrade" in values["Operation"]
                        and package in values["Target"]]
            self.assertEqual(len(matching), 1, package)
        self.successful_update()
        result = self.run_script("rpi4-check-boot", RPI4_IMAGE_BUILD="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = self.successful_update(RPI4_IMAGE_BUILD="0")
        self.assertEqual((selected / "initramfs-linux.img").read_bytes(), b"initramfs rebuilt")

    def test_mount_guard_checks_mount_uuid_type_and_writable_options(self):
        cases = [
            ({"BOOT_TEST_MOUNT_EXIT": "1"}, "Mount"),
            ({"BOOT_TEST_UUID": "1234-5678"}, "UUID"),
            ({"BOOT_TEST_FSTYPE": "ext4"}, "FAT"),
            ({"BOOT_TEST_OPTIONS": "ro,nosuid,nodev"}, "read-only"),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment):
                result = self.run_script("rpi4-check-boot", RPI4_IMAGE_BUILD="0", **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
        result = self.run_script("rpi4-check-boot", RPI4_IMAGE_BUILD="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        # A runtime failure is not bypassed merely because /boot is a directory.
        result = self.run_script(RPI4_IMAGE_BUILD="0", BOOT_TEST_MOUNT_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.entry.exists())

    def test_builder_bypasses_mount_but_rejects_invalid_configuration(self):
        result = self.run_script("rpi4-check-boot", BOOT_TEST_MOUNT_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.config.write_text(self.config.read_text().replace(PARTUUID, "not-a-uuid"))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PARTUUID", result.stderr)
        self.assertFalse(self.entry.exists())

    def test_initial_image_has_consistent_efi_and_initramfs(self):
        boot_set = self.successful_update()
        self.assertEqual((boot_set / "linux.efi").read_bytes(), self.kernel.read_bytes())
        self.assertEqual((boot_set / "bcm2711-rpi-4-b.dtb").read_bytes(), self.dtb.read_bytes())
        self.assertEqual((boot_set / "kernel-version").read_text(), KVER + "\n")
        self.assertEqual(self.fallback.read_bytes(), self.bootloader.read_bytes())
        self.assertEqual(self.vendor.read_bytes(), self.bootloader.read_bytes())
        self.assertIn(f"root=PARTUUID={PARTUUID}", self.entry.read_text())
        self.assertIn("initcall_blacklist=psci_checker", self.entry.read_text())
        self.assertIn(" acpi=on\n", self.entry.read_text())
        self.assertNotIn("module_blacklist=", self.entry.read_text())
        self.assertNotRegex(self.entry.read_text(), r"(?m)^devicetree\s")
        result = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=boot_set, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default rpi4.conf", (self.boot / "loader/loader.conf").read_text())

    def test_invalid_firmware_tables_leave_all_boot_files_unchanged(self):
        self.successful_update()
        configuration = self.config.read_text()
        before = {
            path.relative_to(self.boot): path.read_bytes() if path.is_file() else None
            for path in self.boot.rglob("*")
        }
        self.kernel.write_bytes(efi_image(b"kernel upgrade waiting for valid config"))
        for value in ("", "dt", "ACPI", "devicetree acpi=on"):
            with self.subTest(value=value):
                self.config.write_text(configuration + f"RPI4_FIRMWARE_TABLES={shlex.quote(value)}\n")
                for name in self.scripts:
                    result = self.run_script(name, RPI4_IMAGE_BUILD="0")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid RPI4_FIRMWARE_TABLES", result.stderr)
                after = {
                    path.relative_to(self.boot): path.read_bytes() if path.is_file() else None
                    for path in self.boot.rglob("*")
                }
                self.assertEqual(after, before)

    def test_devicetree_choice_survives_kernel_update_and_can_return_to_acpi(self):
        configuration = self.config.read_text()
        self.config.write_text(configuration + "RPI4_FIRMWARE_TABLES=devicetree\n")
        first = self.successful_update(RPI4_IMAGE_BUILD="0")
        first_options = next(line for line in self.entry.read_text().splitlines() if line.startswith("options "))
        self.assertIn(" acpi=off module_blacklist=vc4,v3d", first_options)

        next_version = "7.2.10-arch1-1"
        next_kernel = self.kernel.parent.parent / next_version / "vmlinuz"
        next_kernel.parent.mkdir()
        next_kernel.write_bytes(efi_image(b"next kernel version"))
        next_dtb = next_kernel.parent / "dtb/broadcom/bcm2711-rpi-4-b.dtb"
        next_dtb.parent.mkdir(parents=True)
        next_dtb.write_bytes(self.dtb.read_bytes())
        update_environment = {
            "RPI4_IMAGE_BUILD": "0",
            "BOOT_TEST_KERNEL": str(next_kernel),
            "BOOT_TEST_KVER": next_version,
        }
        second = self.successful_update(**update_environment)
        self.assertNotEqual(first, second)
        self.assertEqual((second / "kernel-version").read_text(), next_version + "\n")
        entry = self.entry.read_text()
        next_options = next(line for line in entry.splitlines() if line.startswith("options "))
        self.assertEqual(next_options, first_options)
        self.assertNotRegex(entry, r"(?m)^devicetree\s")
        for trial_option in ("panic=", "panic_on_oops=", "systemd.mask=", "rfkill.default_state="):
            self.assertNotIn(trial_option, next_options)

        self.config.write_text(configuration + "RPI4_FIRMWARE_TABLES=acpi\n")
        self.successful_update(**update_environment)
        self.assertIn(" acpi=on\n", self.entry.read_text())
        self.assertNotIn("module_blacklist=", self.entry.read_text())

    def test_absent_firmware_tables_config_keeps_acpi_despite_environment(self):
        self.successful_update(RPI4_FIRMWARE_TABLES="devicetree")
        self.assertIn(" acpi=on\n", self.entry.read_text())
        self.assertNotIn("module_blacklist=", self.entry.read_text())

    def test_absent_boot_mode_keeps_uefi_despite_environment(self):
        self.successful_update(RPI4_BOOT_MODE="native")
        self.assertTrue(self.fallback.exists())
        self.assertFalse((self.boot / "native-current.txt").exists())

    def test_absent_boot_mode_keeps_uefi_checker_validation(self):
        # A native installation ignores this UEFI-only setting. An old card
        # must still validate it, regardless of the new-build environment.
        with self.config.open("a") as stream:
            stream.write("RPI4_FIRMWARE_TABLES=invalid\n")
        for name in self.scripts:
            with self.subTest(name=name):
                result = self.run_script(name, RPI4_BOOT_MODE="native")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid RPI4_FIRMWARE_TABLES", result.stderr)
        self.assertFalse(self.fallback.exists())
        self.assertFalse((self.boot / "native-current.txt").exists())

    def test_invalid_boot_mode_does_not_change_selected_files(self):
        self.successful_update()
        before = {p.relative_to(self.boot): p.read_bytes()
                  for p in self.boot.rglob("*") if p.is_file()}
        config = self.config.read_text()
        for mode in ("", "Native", "both", "native uefi"):
            with self.subTest(mode=mode):
                self.config.write_text(config + f"RPI4_BOOT_MODE={shlex.quote(mode)}\n")
                for name in self.scripts:
                    result = self.run_script(name)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid RPI4_BOOT_MODE", result.stderr)
                after = {p.relative_to(self.boot): p.read_bytes()
                         for p in self.boot.rglob("*") if p.is_file()}
                self.assertEqual(after, before)

    def test_mode_change_while_waiting_for_lock_does_not_run_uefi_backend(self):
        self.successful_update()
        before = {p.relative_to(self.boot): p.read_bytes()
                  for p in self.boot.rglob("*") if p.is_file()}
        self.mock("flock", 'printf "\\nRPI4_BOOT_MODE=native\\n" >> "$BOOT_TEST_CONFIG"\n'
                           'exec /usr/bin/flock "$@"\n')
        result = self.run_script(BOOT_TEST_CONFIG=str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Boot mode changed", result.stderr)
        after = {p.relative_to(self.boot): p.read_bytes()
                 for p in self.boot.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_initramfs_failure_preserves_selected_entry_and_loader(self):
        old_set = self.successful_update()
        old_entry = self.entry.read_bytes()
        old_loader = self.fallback.read_bytes()
        self.kernel.write_bytes(efi_image(b"kernel upgrade"))
        self.bootloader.write_bytes(efi_image(b"systemd upgrade"))
        result = self.run_script(RPI4_IMAGE_BUILD="0", BOOT_TEST_MKINITCPIO_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.entry.read_bytes(), old_entry)
        self.assertEqual(self.fallback.read_bytes(), old_loader)
        self.assertEqual(list((self.boot / "rpi4").iterdir()), [old_set])

    def test_failed_entry_rename_keeps_old_set_and_selection(self):
        old_set = self.successful_update()
        old_entry = self.entry.read_bytes()
        self.kernel.write_bytes(efi_image(b"kernel upgrade"))
        self.mock(
            "mv",
            'if [[ ${!#} == */loader/entries/rpi4.conf ]]; then exit 1; fi\n'
            'exec /usr/bin/mv "$@"\n',
        )
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.entry.read_bytes(), old_entry)
        self.assertTrue(old_set.exists())
        self.assertFalse(list((self.boot / "loader/entries").glob(".rpi4*")))

    def test_mismatched_builder_marker_does_not_select_kernel(self):
        self.marker.write_text("another-kernel\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("initramfs does not match", result.stderr)
        self.assertFalse(self.entry.exists())

    def test_wrong_architecture_and_raw_images_are_rejected(self):
        for payload in (b"raw Image", efi_image(b"x86 kernel", machine=0x8664)):
            with self.subTest(payload=payload[:2]):
                self.kernel.write_bytes(payload)
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.entry.exists())

    def test_wrong_architecture_bootloader_is_rejected(self):
        self.bootloader.write_bytes(efi_image(b"x86 loader", machine=0x8664))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not AArch64", result.stderr)
        self.assertFalse(self.entry.exists())

    def test_missing_dtb_and_ambiguous_package_list_are_rejected(self):
        result = self.run_script(BOOT_TEST_DUPLICATE_KERNEL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)
        self.dtb.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DTB is missing", result.stderr)
        self.assertFalse(self.entry.exists())

    def test_abandoned_staging_is_reclaimed_without_removing_other_paths(self):
        selected = self.successful_update()
        directory = self.boot / "rpi4"
        abandoned = directory / f".{KVER}.Ab12z9"
        abandoned.mkdir()
        (abandoned / "initramfs-linux.img").write_bytes(b"interrupted payload")
        unrelated = directory / ".operator-notes"
        unrelated.mkdir()
        (unrelated / "note").write_text("preserve")
        external = self.root / "outside-boot"
        external.mkdir()
        (external / "sentinel").write_text("preserve")
        linked = directory / f".{KVER}.Zy98x7"
        linked.symlink_to(external, target_is_directory=True)

        self.assertEqual(self.successful_update(), selected)
        self.assertFalse(abandoned.exists())
        self.assertEqual((unrelated / "note").read_text(), "preserve")
        self.assertTrue(linked.is_symlink())
        self.assertEqual((external / "sentinel").read_text(), "preserve")
        self.assertTrue((selected / "linux.efi").is_file())

    def test_update_retains_one_previous_payload_without_promising_rollback(self):
        first = self.successful_update()
        self.kernel.write_bytes(efi_image(b"kernel two"))
        second = self.successful_update()
        self.assertNotEqual(second, first)
        self.assertIn(f"# previous-boot-set {first.name}", self.entry.read_text())
        self.assertEqual(self.successful_update(), second)
        self.assertTrue(first.exists(), "idempotent refresh must retain previous payload")
        self.kernel.write_bytes(efi_image(b"kernel three"))
        third = self.successful_update()
        self.assertFalse(first.exists())
        self.assertEqual(set((self.boot / "rpi4").iterdir()), {second, third})
        self.assertEqual(list(self.entry.parent.glob("*.conf")), [self.entry])
        self.assertIn("not an OS rollback", self.entry.read_text())

    def test_damaged_existing_set_is_not_reused(self):
        boot_set = self.successful_update()
        old_entry = self.entry.read_bytes()
        (boot_set / "linux.efi").write_bytes(b"damaged")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("boot set is damaged", result.stderr)
        self.assertEqual(self.entry.read_bytes(), old_entry)

    def test_runtime_refresh_rebuilds_initramfs_and_preserves_loader_preferences(self):
        old_set = self.successful_update()
        config = self.boot / "loader/loader.conf"
        config.write_text("default rpi4.conf\ntimeout 9\n")
        self.bootloader.write_bytes(efi_image(b"systemd upgrade"))
        new_set = self.successful_update(RPI4_IMAGE_BUILD="0")
        self.assertNotEqual(new_set, old_set)
        self.assertEqual((new_set / "initramfs-linux.img").read_bytes(), b"initramfs rebuilt")
        self.assertEqual(config.read_text(), "default rpi4.conf\ntimeout 9\n")
        self.assertEqual(self.fallback.read_bytes(), self.bootloader.read_bytes())
        self.assertEqual(self.vendor.read_bytes(), self.bootloader.read_bytes())


if __name__ == "__main__":
    unittest.main()
