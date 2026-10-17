"""Check new-build defaults and explicit legacy routing without building images."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
CASES = ((None, (), "native"), ("", (), "native"), ("uefi", (), "uefi"),
         (None, ("--boot-mode", "uefi"), "uefi"),
         ("native", ("--boot-mode", "uefi"), "uefi"),
         ("uefi", ("--boot-mode", "native"), "native"))


def environment(mode):
    result = os.environ.copy()
    result.pop("RPI4_BOOT_MODE", None)
    if mode is not None:
        result["RPI4_BOOT_MODE"] = mode
    return result


def module(filename):
    spec = importlib.util.spec_from_file_location("defaults_" + filename, PROJECT / "scripts" / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def image_name(mode):
    return "rpi4-archlinux-ports-" + ("native-" if mode == "native" else "") + "sd-usb.img"


def mode_path(project, top, mode):
    return project / top / "native" if mode == "native" else project / top


class StopBeforeWork(Exception):
    pass


class DefaultRoutingTests(unittest.TestCase):
    def test_invalid_environment_is_rejected_before_paths_or_work(self):
        for filename, boundary in (("make-images.py", "configure_mode"),
                                   ("verify-images.py", "verify_image"),
                                   ("verify-qemu.py", "sha256"),
                                   ("write-input-manifest.py", "manifest")):
            with self.subTest(script=filename), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                target = module(filename)
                arguments = (["--verified-fingerprint", "test"] if filename == "write-input-manifest.py" else [])
                errors = io.StringIO()
                with patch.dict(os.environ, environment("natve"), clear=True), \
                        patch.object(sys, "argv", [filename, *arguments]), \
                        patch.object(target, "REPO" if filename == "verify-qemu.py" else "PROJECT", project), \
                        patch.object(target, boundary, side_effect=AssertionError("Work started before validation")) as work, \
                        redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as error:
                        target.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("RPI4_BOOT_MODE must be native or uefi", errors.getvalue())
                work.assert_not_called()
                self.assertEqual(list(project.iterdir()), [])

    def test_shell_driver_propagates_mode_and_isolates_outputs(self):
        for env_mode, arguments, expected in CASES:
            with self.subTest(environment=env_mode, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                (project / "scripts").mkdir()
                (project / "commands").mkdir()
                for name in ("build-image.sh", "build-paths.sh"):
                    shutil.copy2(PROJECT / "scripts" / name, project / "scripts" / name)
                for name in ("preflight.sh", "prepare-tools.sh", "build-rootfs.sh"):
                    stub = project / "scripts" / name
                    stub.write_text('#!/bin/bash\nset -eu\nmkdir -p "$RPI4_BUILD_DIR/logs"\n'
                                    'printf "%s:%s:%s\\n" "${0##*/}" "$RPI4_BOOT_MODE" "$RPI4_ROOTFS_DIR" >> calls\n')
                    stub.chmod(0o755)
                recorder = (f"#!{sys.executable}\nimport json,os,sys\n"
                            "with open('commands.jsonl', 'a') as stream:\n"
                            "    stream.write(json.dumps([os.environ['RPI4_BOOT_MODE'], sys.argv[1:]]) + '\\n')\n")
                (project / "scripts/verify-images.py").write_text(recorder)
                podman = project / "commands/podman"
                podman.write_text(recorder)
                podman.chmod(0o755)
                env = environment(env_mode)
                env["PATH"] = str(project / "commands") + os.pathsep + env["PATH"]
                result = subprocess.run(["bash", project / "scripts/build-image.sh", *arguments],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                relative_root = str(mode_path(Path(), "build", expected) / "rootfs")
                self.assertEqual((project / "calls").read_text().splitlines(),
                                 [f"{name}:{expected}:{relative_root}" for name in
                                  ("preflight.sh", "prepare-tools.sh", "build-rootfs.sh")])
                self.assertEqual([json.loads(line) for line in (project / "commands.jsonl").read_text().splitlines()],
                                 [[expected, ["unshare", "python3", "scripts/make-images.py", "--boot-mode", expected]],
                                  [expected, ["--boot-mode", expected]]])
                other = "uefi" if expected == "native" else "native"
                self.assertFalse((mode_path(project, "build", other) / "logs").exists())

    def test_assembler_cli_selects_mode_before_any_mutation(self):
        make = module("make-images.py")
        for env_mode, arguments, expected in CASES:
            with self.subTest(environment=env_mode, arguments=arguments), \
                    patch.dict(os.environ, environment(env_mode), clear=True), \
                    patch.object(sys, "argv", ["make-images.py", *arguments]), \
                    patch.object(make, "require_user_namespace", side_effect=StopBeforeWork):
                with self.assertRaises(StopBeforeWork):
                    make.main()
                self.assertEqual(make.BOOT_MODE, expected)
                self.assertEqual(make.ROOT, mode_path(PROJECT, "build", expected) / "rootfs")
                self.assertEqual(make.ARTIFACTS, mode_path(PROJECT, "artifacts", expected))
                self.assertEqual(make.IMAGE_NAME, image_name(expected))

    def test_offline_cli_selects_image_and_report_directory(self):
        verify = module("verify-images.py")
        for env_mode, arguments, expected in CASES:
            with self.subTest(environment=env_mode, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                with patch.dict(os.environ, environment(env_mode), clear=True), \
                        patch.object(sys, "argv", ["verify-images.py", *arguments]), \
                        patch.multiple(verify, PROJECT=project, ARTIFACTS=project / "artifacts"), \
                        patch.object(verify.shutil, "which", return_value="fixture-tool"), \
                        patch.object(verify, "verify_image", return_value={"passed": True}) as inspect, \
                        redirect_stdout(io.StringIO()):
                    verify.main()
                inspect.assert_called_once_with(mode_path(project, "artifacts", expected) / image_name(expected), False)
                report = mode_path(project, "build", expected) / "image-verification/report.json"
                self.assertEqual(json.loads(report.read_text()), [{"passed": True}])

    def test_qemu_cli_selects_image_before_launching_tools(self):
        qemu = module("verify-qemu.py")
        for env_mode, arguments, expected in CASES:
            with self.subTest(environment=env_mode, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                for mode in ("native", "uefi"):
                    image = mode_path(project, "artifacts", mode) / image_name(mode)
                    image.parent.mkdir(parents=True, exist_ok=True)
                    image.write_bytes(b"routing fixture")
                with patch.dict(os.environ, environment(env_mode), clear=True), \
                        patch.object(sys, "argv", ["verify-qemu.py", *arguments]), \
                        patch.object(qemu, "REPO", project), \
                        patch.object(qemu, "sha256", side_effect=StopBeforeWork) as checksum, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(StopBeforeWork):
                        qemu.main()
                checksum.assert_called_once_with(mode_path(project, "artifacts", expected) / image_name(expected))
                self.assertEqual(len(list(mode_path(project, "build", expected).glob("qemu-test-*"))), 1)
        self.assertIn("[[ ! -d /sys/firmware/efi ]]", qemu.guest_script("test", "root", "boot", 1024, 900))

    def test_input_manifest_cli_uses_selected_provenance_directory(self):
        writer = module("write-input-manifest.py")
        for env_mode, arguments, expected in CASES:
            with self.subTest(environment=env_mode, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                with patch.dict(os.environ, environment(env_mode), clear=True), \
                        patch.object(sys, "argv", ["write-input-manifest.py", "--verified-fingerprint", "test", *arguments]), \
                        patch.object(writer, "PROJECT", project), \
                        patch.object(writer, "manifest", return_value={"boot_mode": expected}) as manifest, \
                        redirect_stdout(io.StringIO()):
                    writer.main()
                manifest.assert_called_once_with("test", expected)
                output = mode_path(project, "artifacts", expected) / "input-manifest.json"
                self.assertEqual(json.loads(output.read_text()), {"boot_mode": expected})


if __name__ == "__main__":
    unittest.main()
