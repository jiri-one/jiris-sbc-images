"""Exercise firmware download, cache checks and installation without building an image."""

import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


PROJECT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("make_images_runtime", PROJECT / "scripts/make-images.py")
MAKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAKE)
FINGERPRINT = "1" * 40
COMMIT = "2" * 40
WIFI_COMMIT = "4" * 40
WIFI_BOARD = "brcmfmac43455-sdio.raspberrypi,4-model-b"


def digest(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(all(shutil.which(tool) for tool in ("curl", "bsdtar", "dtc")),
                     "curl, bsdtar and dtc required")
class RuntimeFirmwareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "project"
        for directory in ("scripts", "configs", "fixtures/runtime", "fixtures/wifi/cypress",
                          "fixtures/wifi/brcm", "commands"):
            (self.workspace / directory).mkdir(parents=True)
        for name in ("fetch-inputs.sh", "write-input-manifest.py", "build-paths.sh"):
            shutil.copy2(PROJECT / "scripts" / name, self.workspace / "scripts" / name)
        shutil.copy2(PROJECT / "configs/pacman.conf", self.workspace / "configs/pacman.conf")
        shutil.copytree(PROJECT / "configs/overlays", self.workspace / "configs/overlays")
        self.fixtures = self.workspace / "fixtures"
        bootstrap = b"test bootstrap input"
        (self.fixtures / "bootstrap.tar.zst").write_bytes(bootstrap)
        (self.fixtures / "signer.asc").write_text("test public key bundle")
        self.bundle = {
            "RPI_EFI.fd": b"unchanged UEFI firmware",
            "config.txt": b"arm_64bit=1\r\ndtoverlay=upstream-pi4\r\n",
            "bcm2711-rpi-4-b.dtb": b"unchanged device tree",
            "overlays/upstream-pi4.dtbo": b"unchanged overlay",
            "start4.elf": b"older bundled runtime",
            "fixup4.dat": b"older bundled fixup",
        }
        archive = self.fixtures / "uefi.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name, data in self.bundle.items():
                output.writestr(name, data)
        self.runtime = {
            "start4.elf": b"new released runtime",
            "fixup4.dat": b"matching released fixup",
        }
        for name, data in self.runtime.items():
            (self.fixtures / "runtime" / name).write_bytes(data)
        self.wifi = {
            "cypress/cyfmac43455-sdio-standard.bin": (f"{WIFI_BOARD}.bin", b"tested Wi-Fi firmware"),
            "cypress/cyfmac43455-sdio.clm_blob": (f"{WIFI_BOARD}.clm_blob", b"tested regulatory blob"),
            "brcm/brcmfmac43455-sdio.txt": (f"{WIFI_BOARD}.txt", b"tested NVRAM\n"),
        }
        for source, (_, data) in self.wifi.items():
            (self.fixtures / "wifi" / source).write_bytes(data)
        pins = {
            "BOOTSTRAP_NAME": "bootstrap.tar.zst",
            "BOOTSTRAP_BASE": self.fixtures.as_uri(),
            "BOOTSTRAP_SHA256": digest(bootstrap),
            "PORTS_KEY_URL": (self.fixtures / "signer.asc").as_uri(),
            "PORTS_KEY_FINGERPRINT": FINGERPRINT,
            "FIRMWARE_VERSION": "v1.53",
            "FIRMWARE_NAME": "uefi.zip",
            "FIRMWARE_URL": archive.as_uri(),
            "FIRMWARE_SHA256": digest(archive.read_bytes()),
            "RUNTIME_FIRMWARE_VERSION": "test-release",
            "RUNTIME_FIRMWARE_COMMIT": COMMIT,
            "RUNTIME_FIRMWARE_BASE": (self.fixtures / "runtime").as_uri(),
            "RUNTIME_START4_SHA256": digest(self.runtime["start4.elf"]),
            "RUNTIME_FIXUP4_SHA256": digest(self.runtime["fixup4.dat"]),
            "WIFI_FIRMWARE_COMMIT": WIFI_COMMIT,
            "WIFI_FIRMWARE_BASE": (self.fixtures / "wifi").as_uri(),
            "WIFI_BIN_SHA256": digest(self.wifi["cypress/cyfmac43455-sdio-standard.bin"][1]),
            "WIFI_CLM_SHA256": digest(self.wifi["cypress/cyfmac43455-sdio.clm_blob"][1]),
            "WIFI_NVRAM_SHA256": digest(self.wifi["brcm/brcmfmac43455-sdio.txt"][1]),
            "TOOLS_BASE": "example.invalid/tools@sha256:" + "3" * 64,
        }
        (self.workspace / "configs/sources.env").write_text(
            "".join(f"{name}={shlex.quote(value)}\n" for name, value in pins.items()))
        # Isolate package-signer handling; these tests exercise firmware, not GPG.
        gpg = self.workspace / "commands/gpg"
        gpg.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "if '--export' in sys.argv:\n"
            "    print('test selected public key')\n"
            "elif '--show-keys' in sys.argv:\n"
            f"    print('pub:::::::::\\nfpr:::::::::{FINGERPRINT}:')\n"
            "elif '--import' not in sys.argv:\n"
            "    sys.exit('Unexpected GPG invocation')\n")
        gpg.chmod(0o755)
        self.cache = self.workspace / "cache" / f"raspberrypi-firmware-{COMMIT}"
        self.staging = self.workspace / "build/firmware"
        self.wifi_cache = self.workspace / "cache" / f"raspberrypi-wifi-firmware-{WIFI_COMMIT}"
        self.wifi_staging = self.workspace / "build/wifi-firmware"
        self.manifest_path = self.workspace / "artifacts/input-manifest.json"

    def fetch(self, boot_mode="uefi", arguments=()):
        environment = os.environ.copy()
        if boot_mode is None:
            environment.pop("RPI4_BOOT_MODE", None)
        else:
            environment["RPI4_BOOT_MODE"] = boot_mode
        environment["PATH"] = str(self.workspace / "commands") + os.pathsep + environment["PATH"]
        return subprocess.run(["bash", str(self.workspace / "scripts/fetch-inputs.sh"), *arguments],
                              cwd=self.workspace, env=environment, capture_output=True,
                              text=True, timeout=20)

    def fetch_successfully(self):
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(self.manifest_path.read_text())

    def install(self, runtime):
        root = self.workspace / "build/rootfs"
        (root / "boot").mkdir(parents=True, exist_ok=True)
        with patch.multiple(MAKE, PROJECT=self.workspace, ROOT=root, FIRMWARE=self.staging,
                            BOOT_MODE="uefi"):
            return MAKE.install_firmware(runtime)

    def test_fetch_replaces_only_matched_pair_and_install_preserves_firmware_blobs(self):
        manifest = self.fetch_successfully()
        runtime = manifest["runtime_firmware"]
        self.assertEqual(runtime["version"], "test-release")
        self.assertEqual(runtime["commit"], COMMIT)
        self.assertEqual(manifest["firmware"]["version"], "v1.53")
        for name, data in self.runtime.items():
            self.assertEqual(runtime["files"][name]["sha256"], digest(data))
            self.assertEqual(runtime["files"][name]["url"],
                             (self.fixtures / "runtime" / name).as_uri())
        hashes = self.install(runtime)
        for name, data in (self.bundle | self.runtime).items():
            with self.subTest(name=name):
                self.assertEqual((self.staging / name).read_bytes(), data)
                installed = (data + b"dtoverlay=rpi4-usb-c-data-disable\r\n"
                             b"max_framebuffer_width=4096\r\nmax_framebuffer_height=2160\r\n"
                             if name == "config.txt" else data)
                self.assertEqual((self.workspace / "build/rootfs/boot" / name).read_bytes(), installed)
                self.assertEqual(hashes[name], digest(installed))
        overlay_name = "overlays/rpi4-usb-c-data-disable.dtbo"
        overlay = self.workspace / "build/rootfs/boot" / overlay_name
        self.assertEqual(hashes[overlay_name], digest(overlay.read_bytes()))
        self.assertEqual(self.install(runtime), hashes)

    @unittest.skipUnless(shutil.which("fdtoverlay") and shutil.which("fdtget"),
                         "fdtoverlay and fdtget required")
    def test_installed_overlay_changes_only_usb_c_controller_status(self):
        manifest = self.fetch_successfully()
        self.install(manifest["runtime_firmware"])
        source = self.fixtures / "board.dts"
        source.write_text('''/dts-v1/;
/ {
    compatible = "raspberrypi,4-model-b", "brcm,bcm2711";
    usbphy: phy { compatible = "usb-nop-xceiv"; #phy-cells = <0>; };
    soc {
        usb: usb-controller {
            compatible = "brcm,bcm2835-usb";
            status = "okay";
            phys = <&usbphy>;
            dr_mode = "otg";
        };
    };
    scb {
        pcie { status = "okay"; usb-controller { status = "okay"; }; };
        ethernet { compatible = "brcm,bcm2711-genet-v5"; status = "okay"; };
    };
    v3d { compatible = "brcm,2711-v3d"; status = "okay"; };
};
''')
        base, merged = self.fixtures / "board.dtb", self.fixtures / "merged.dtb"
        subprocess.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", base, source],
                       check=True, capture_output=True)
        overlay = self.workspace / "build/rootfs/boot/overlays/rpi4-usb-c-data-disable.dtbo"
        subprocess.run(["fdtoverlay", "-i", base, "-o", merged, overlay],
                       check=True, capture_output=True)
        trees = [subprocess.run(["dtc", "-s", "-I", "dtb", "-O", "dts", path],
                                check=True, capture_output=True, text=True).stdout
                 for path in (base, merged)]
        changes = [line for line in difflib.ndiff(*(tree.splitlines() for tree in trees))
                   if line.startswith(("- ", "+ "))]
        self.assertEqual(changes, ['- \t\t\tstatus = "okay";',
                                   '+ \t\t\tstatus = "disabled";'])
        status = subprocess.run(["fdtget", "-t", "s", merged, "/soc/usb-controller", "status"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(status, "disabled")

    def test_missing_upstream_overlay_aborts_before_boot_files_are_written(self):
        manifest = self.fetch_successfully()
        (self.staging / "config.txt").write_text("arm_64bit=1\n")
        with self.assertRaisesRegex(ValueError, "exactly one upstream-pi4 overlay"):
            self.install(manifest["runtime_firmware"])
        self.assertFalse(any((self.workspace / "build/rootfs/boot").iterdir()))

    def test_overlay_compiler_failure_aborts_before_boot_files_are_written(self):
        manifest = self.fetch_successfully()
        (self.workspace / "configs/overlays/rpi4-usb-c-data-disable-overlay.dts").write_text(
            "not a device tree")
        with self.assertRaises(subprocess.CalledProcessError):
            self.install(manifest["runtime_firmware"])
        self.assertFalse(any((self.workspace / "build/rootfs/boot").iterdir()))

    def test_verified_cache_can_be_reused_without_sources(self):
        self.fetch_successfully()
        shutil.rmtree(self.fixtures)
        self.fetch_successfully()
        for name, data in self.runtime.items():
            self.assertEqual((self.staging / name).read_bytes(), data)

    def test_native_fetch_keeps_existing_uefi_manifest(self):
        self.fetch_successfully()
        original = self.manifest_path.read_bytes()
        result = self.fetch("native")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.manifest_path.read_bytes(), original)
        native = self.workspace / "artifacts/native/input-manifest.json"
        self.assertEqual(json.loads(native.read_text())["runtime_firmware"]["commit"], COMMIT)

    def test_default_native_fetch_needs_no_uefi_archive(self):
        (self.fixtures / "uefi.zip").unlink()
        result = self.fetch(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        native = self.workspace / "artifacts/native/input-manifest.json"
        manifest = json.loads(native.read_text())
        self.assertEqual(manifest["boot_mode"], "native")
        self.assertEqual(manifest["runtime_firmware"]["commit"], COMMIT)
        self.assertNotIn("firmware", manifest)
        self.assertFalse(self.manifest_path.exists())
        self.assertFalse((self.workspace / "cache/uefi.zip").exists())
        self.assertEqual({path.name for path in self.staging.iterdir()}, set(self.runtime))

    def test_explicit_uefi_fetch_overrides_native_environment(self):
        result = self.fetch("native", ("--boot-mode", "uefi"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(manifest["boot_mode"], "uefi")
        self.assertEqual(manifest["firmware"]["version"], "v1.53")
        self.assertEqual((self.staging / "RPI_EFI.fd").read_bytes(), self.bundle["RPI_EFI.fd"])
        self.assertFalse((self.workspace / "artifacts/native/input-manifest.json").exists())

    def test_native_install_discards_snapshot_boot_files_and_preserves_new_initramfs(self):
        manifest = self.fetch_successfully()
        shutil.copytree(PROJECT / "configs/native", self.workspace / "configs/native")
        license_name = "overlay/usr/share/licenses/rpi4-uefi-firmware/RaspberryPi-LICENCE.broadcom"
        license_path = self.workspace / license_name
        license_path.parent.mkdir(parents=True)
        shutil.copy2(PROJECT / license_name, license_path)
        root = self.workspace / "build/native/rootfs"
        (root / "boot/native/old").mkdir(parents=True)
        (root / "boot/native/old/cmdline.txt").write_text("root=PARTUUID=old")
        (root / "boot/RPI_EFI.fd").write_bytes(b"old firmware")
        (root / "boot/native-current.txt").write_text("old selection")
        (root / "boot/initramfs-linux.img").write_bytes(b"new native initramfs")
        with patch.multiple(MAKE, PROJECT=self.workspace, ROOT=root, FIRMWARE=self.staging,
                            BOOT_MODE="native"):
            hashes = MAKE.install_firmware(manifest["runtime_firmware"])
        self.assertEqual((root / "boot/initramfs-linux.img").read_bytes(), b"new native initramfs")
        self.assertFalse((root / "boot/native").exists())
        self.assertFalse((root / "boot/native-current.txt").exists())
        self.assertFalse((root / "boot/RPI_EFI.fd").exists())
        self.assertEqual(set(hashes), {"start4.elf", "fixup4.dat", "config.txt", "LICENCE.broadcom",
                                      "overlays/rpi4-usb-c-data-disable.dtbo"})
        self.assertEqual((root / "boot/config.txt").read_bytes(),
                         (PROJECT / "configs/native/config.txt").read_bytes())
        pin = json.loads((root / "usr/share/rpi4/runtime-firmware.json").read_text())
        self.assertEqual(pin["commit"], COMMIT)
        self.assertEqual(pin["sha256"], {name: digest(data) for name, data in self.runtime.items()})

    def test_corrupt_cached_pair_aborts_before_replacing_verified_stage(self):
        self.fetch_successfully()
        manifest_before = self.manifest_path.read_bytes()
        for name, data in self.runtime.items():
            with self.subTest(name=name):
                (self.cache / name).write_bytes(b"corrupted cached input")
                result = self.fetch()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{name}: FAILED", result.stdout)
                self.assertEqual(self.manifest_path.read_bytes(), manifest_before)
                for staged_name, expected in (self.bundle | self.runtime).items():
                    self.assertEqual((self.staging / staged_name).read_bytes(), expected)
                (self.cache / name).write_bytes(data)

    def test_missing_download_never_stages_half_a_pair(self):
        (self.fixtures / "runtime/fixup4.dat").unlink()
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(self.staging.iterdir()))
        self.assertFalse(self.manifest_path.exists())

    def test_changed_stage_is_rejected_before_boot_files_are_written(self):
        manifest = self.fetch_successfully()
        (self.staging / "start4.elf").write_bytes(self.bundle["start4.elf"])
        with self.assertRaisesRegex(ValueError, "Staged runtime firmware changed: start4.elf"):
            self.install(manifest["runtime_firmware"])
        self.assertFalse(any((self.workspace / "build/rootfs/boot").iterdir()))

    def test_incomplete_pair_provenance_is_rejected(self):
        manifest = self.fetch_successfully()
        del manifest["runtime_firmware"]["files"]["fixup4.dat"]
        with self.assertRaisesRegex(ValueError, "matched start4/fixup4 pair"):
            self.install(manifest["runtime_firmware"])
        self.assertFalse(any((self.workspace / "build/rootfs/boot").iterdir()))


    def install_wifi(self, wifi_firmware):
        root = self.workspace / "build/native/rootfs"
        (root / "usr/share/rpi4").mkdir(parents=True, exist_ok=True)
        with patch.multiple(MAKE, PROJECT=self.workspace, ROOT=root,
                            WIFI_FIRMWARE=self.wifi_staging, BOOT_MODE="native"):
            return root, MAKE.install_wifi_firmware(wifi_firmware)

    def test_wifi_firmware_is_staged_under_board_names_and_recorded(self):
        manifest = self.fetch_successfully()
        wifi = manifest["wifi_firmware"]
        self.assertEqual(wifi["commit"], WIFI_COMMIT)
        self.assertEqual(set(wifi["files"]), set(MAKE.WIFI_NAMES))
        for source, (installed, data) in self.wifi.items():
            with self.subTest(source=source):
                self.assertEqual(wifi["files"][installed],
                                 {"url": (self.fixtures / "wifi" / source).as_uri(), "sha256": digest(data)})
                self.assertEqual((self.wifi_staging / installed).read_bytes(), data)
        root, hashes = self.install_wifi(wifi)
        target = root / "usr/lib/firmware/updates/brcm"
        (target / "other-override.bin").write_bytes(b"left in a snapshot")
        self.assertEqual(self.install_wifi(wifi)[1], hashes)
        self.assertEqual(sorted(path.name for path in target.iterdir()), sorted(MAKE.WIFI_NAMES))
        for installed, data in self.wifi.values():
            self.assertEqual((target / installed).read_bytes(), data)
            self.assertEqual((target / installed).stat().st_mode & 0o777, 0o644)
            self.assertEqual(hashes[installed], digest(data))
        pin = json.loads((root / "usr/share/rpi4/wifi-firmware.json").read_text())
        self.assertEqual(pin, {"commit": WIFI_COMMIT, "sha256": hashes})

    def test_wifi_firmware_changes_are_rejected(self):
        manifest = self.fetch_successfully()
        before = self.manifest_path.read_bytes()
        source = "brcm/brcmfmac43455-sdio.txt"
        installed, data = self.wifi[source]
        (self.wifi_cache / Path(source).name).write_bytes(b"corrupted cached NVRAM")
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("brcmfmac43455-sdio.txt: FAILED", result.stdout)
        self.assertEqual(self.manifest_path.read_bytes(), before)
        self.assertEqual((self.wifi_staging / installed).read_bytes(), data)
        (self.wifi_staging / installed).write_bytes(b"changed stage")
        with self.assertRaisesRegex(ValueError, "Staged Wi-Fi firmware changed"):
            self.install_wifi(manifest["wifi_firmware"])
        del manifest["wifi_firmware"]["files"][installed]
        with self.assertRaisesRegex(ValueError, "firmware, CLM and NVRAM"):
            self.install_wifi(manifest["wifi_firmware"])


if __name__ == "__main__":
    unittest.main()
