"""Adaptive font checks with fake framebuffer/console tools; no host VT access."""

import gzip
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "overlay/usr/local/sbin/rpi4-vconsole-font"
HOOK = PROJECT / "overlay/etc/initcpio/install/rpi4-consolefont"


class ConsoleFontTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rpi4-font-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "vconsole.conf"
        self.config.write_text("KEYMAP=us\n")
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("console=tty1\n")
        self.credentials = self.root / "credentials"
        self.credentials.mkdir()
        self.consoles = self.root / "dev"
        self.consoles.mkdir()
        for number in range(1, 7):
            # Only the character-device check uses this; mocked setfont never
            # opens it or follows it to a real console.
            (self.consoles / f"tty{number}").symlink_to("/dev/null")
        source = HELPER.read_text()
        for path, replacement in (
            ("/etc/vconsole.conf", self.config),
            ("/proc/cmdline", self.cmdline),
            ("/dev/tty", self.consoles / "tty"),
        ):
            source = source.replace(path, str(replacement))
        self.helper = self.root / "helper"
        self.helper.write_text(source)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        mock = self.bin / "mock"
        mock.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "name = pathlib.Path(sys.argv[0]).name\n"
            "args = sys.argv[1:]\n"
            "log = pathlib.Path(os.environ['FONT_TEST_LOG'])\n"
            "previous = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []\n"
            "with log.open('a') as out: out.write(json.dumps([name, args]) + '\\n')\n"
            "if name == 'fbset':\n"
            "    if os.environ.get('FONT_TEST_HEADLESS'): sys.exit(1)\n"
            "    choices = os.environ.get('FONT_TEST_GEOMETRY', '1920 1280').split(';')\n"
            "    count = sum(row[0] == 'fbset' for row in previous)\n"
            "    geometry = choices[min(count, len(choices) - 1)]\n"
            "    print('mode test\\n    geometry ' + geometry + ' 7680 4320 32\\nendmode')\n"
            "elif name == 'setfont':\n"
            "    tty = pathlib.Path(args[1]).name\n"
            "    if '--check' in args:\n"
            "        if tty == os.environ.get('FONT_TEST_GRAPHICS'): sys.exit(1)\n"
            "        if tty == 'tty1' and os.environ.get('FONT_TEST_EARLY_DUMMY'):\n"
            "            if not any(row == [name, args] for row in previous): sys.exit(1)\n"
            "    elif '-d' in args and os.environ.get('FONT_TEST_REJECT_DOUBLED'): sys.exit(1)\n"
        )
        mock.chmod(0o755)
        for name in ("fbset", "setfont", "sleep"):
            (self.bin / name).symlink_to(mock)

    def run_helper(self, **extra):
        self.log.unlink(missing_ok=True)
        environment = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "FONT_TEST_LOG": str(self.log)}
        environment.pop("CREDENTIALS_DIRECTORY", None)
        result = subprocess.run(
            ["sh", str(self.helper)], env={**environment, **extra}, text=True,
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @staticmethod
    def fonts(records):
        return [args for name, args in records if name == "setfont" and "--check" not in args]

    def test_readable_sizes_use_visible_geometry_and_all_six_consoles(self):
        for geometry, expected in (
            ("3840 2160", ["-d", "latarcyrheb-sun32"]),
            ("2560 1440", ["-d", "ter-v22b"]),
            ("1760 1320", ["-d", "ter-v22b"]),
            ("1759 1320", ["latarcyrheb-sun32"]),
            ("1920 1280", ["latarcyrheb-sun32"]),
            ("1920 1080", ["latarcyrheb-sun32"]),
            ("1280 720", ["ter-v22b"]),
            ("880 660", ["ter-v22b"]),
            ("879 660", ["latarcyrheb-sun16"]),
            ("800 600", ["latarcyrheb-sun16"]),
        ):
            with self.subTest(geometry=geometry):
                records = self.run_helper(FONT_TEST_GEOMETRY=geometry)
                fonts = self.fonts(records)
                self.assertEqual({tuple(args[2:]) for args in fonts}, {tuple(expected)})
                self.assertEqual({Path(args[1]).name for args in fonts}, {f"tty{i}" for i in range(1, 7)})
                self.assertEqual(sum(name == "fbset" for name, _ in records), 2)

    def test_font_configuration_and_credentials_are_left_to_systemd(self):
        for setting in ("FONT", "FONT_MAP", "FONT_UNIMAP", "FONT_SCALE"):
            with self.subTest(config=setting):
                self.config.write_text(f"KEYMAP=us\n{setting}=custom\n")
                self.assertEqual(self.run_helper(), [])
        self.config.write_text("KEYMAP=us\n")
        for setting in ("font", "font_map", "font_unimap", "font_scale"):
            with self.subTest(credential=setting):
                credential = self.credentials / f"vconsole.{setting}"
                credential.touch()
                self.assertEqual(self.run_helper(CREDENTIALS_DIRECTORY=str(self.credentials)), [])
                credential.unlink()

    def test_cmdline_overrides_include_initrd_and_legacy_spellings(self):
        for setting in ("font", "font_map", "font_unimap", "font_scale", "font.map", "font.unimap"):
            for prefix in ("", "rd."):
                with self.subTest(setting=setting, prefix=prefix):
                    self.cmdline.write_text(f'console=tty1 {prefix}vconsole.{setting}="custom font"\n')
                    self.assertEqual(self.run_helper(), [])

    def test_keyboard_only_overrides_preserve_adaptive_fonts(self):
        self.config.write_text("KEYMAP=cz\nKEYMAP_TOGGLE=us\n")
        self.cmdline.write_text("vconsole.keymap=de rd.vconsole.keymap_toggle=us\n")
        (self.credentials / "vconsole.keymap").write_text("fr")
        self.assertTrue(self.fonts(self.run_helper(CREDENTIALS_DIRECTORY=str(self.credentials))))

    def test_repeat_tall_font_setup_and_smaller_driver_fallback(self):
        for _ in range(2):
            records = self.run_helper(FONT_TEST_GEOMETRY="3840 2160")
            self.assertTrue(all(args[2:] == ["-d", "latarcyrheb-sun32"] for args in self.fonts(records)))
        fonts = self.fonts(self.run_helper(FONT_TEST_GEOMETRY="3840 2160", FONT_TEST_REJECT_DOUBLED="1"))
        self.assertEqual(fonts[-1][2:], ["latarcyrheb-sun32"])

    def test_graphics_console_is_skipped_and_early_dummy_console_retried(self):
        fonts = self.fonts(self.run_helper(FONT_TEST_GRAPHICS="tty6", FONT_TEST_EARLY_DUMMY="1"))
        self.assertNotIn("tty6", [Path(args[1]).name for args in fonts])
        self.assertEqual(sum(Path(args[1]).name == "tty1" for args in fonts), 1)

    def test_resolution_takeover_gets_settled_font(self):
        records = self.run_helper(FONT_TEST_GEOMETRY="800 600;1920 1280")
        fonts = self.fonts(records)
        self.assertEqual(fonts[0][-1], "latarcyrheb-sun16")
        self.assertEqual(fonts[-1][-1], "latarcyrheb-sun32")
        self.assertEqual(sum(name == "fbset" for name, _ in records), 3)

    def test_headless_and_invalid_geometry_exit_quietly_with_bounded_retry(self):
        for extra in ({"FONT_TEST_HEADLESS": "1"}, {"FONT_TEST_GEOMETRY": "0 0"}, {"FONT_TEST_GEOMETRY": "bad invalid"}):
            with self.subTest(extra=extra):
                records = self.run_helper(**extra)
                self.assertEqual(self.fonts(records), [])
                self.assertEqual(sum(name == "fbset" for name, _ in records), 20)
                self.assertEqual(sum(name == "sleep" for name, _ in records), 19)


class ConsoleFontHookTests(unittest.TestCase):
    def test_mixed_font_suffixes_preserve_unicode_and_missing_font_fails(self):
        with tempfile.TemporaryDirectory(prefix="rpi4-font-hook-") as temporary:
            root = Path(temporary)
            fonts = root / "fonts"
            fonts.mkdir()
            output = root / "output"
            output.mkdir()
            # A minimal Unicode PSF2 with an accented Czech character. The hook
            # must copy the entire decompressed font, not only its bitmap.
            payload = struct.pack("<8I", 0x864AB572, 0, 32, 1, 2, 44, 22, 11)
            payload += bytes(88) + b"A\xff" + "č".encode() + b"\xff"
            names = ("latarcyrheb-sun16.psfu", "ter-v22b.psf", "latarcyrheb-sun32.psfu")
            for name in names:
                (fonts / f"{name}.gz").write_bytes(gzip.compress(payload))
            hook = root / "hook"
            hook.write_text(HOOK.read_text().replace("/usr/share/kbd/consolefonts", str(fonts)))
            runner = """
add_binary() { :; }
add_file() {
    if [[ $1 == - ]]; then
        cat > "$FONT_TEST_OUTPUT/${2##*/}"
    fi
}
source "$1"
build
"""
            command = ["bash", "-c", runner, "font-hook-test", str(hook)]
            environment = {**os.environ, "FONT_TEST_OUTPUT": str(output)}
            result = subprocess.run(command, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({path.name for path in output.iterdir()}, set(names))
            for name in names:
                self.assertEqual((output / name).read_bytes(), payload)
            (fonts / "ter-v22b.psf.gz").unlink()
            result = subprocess.run(command, env=environment, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ter-v22b.psf.gz", result.stderr)


if __name__ == "__main__":
    unittest.main()
