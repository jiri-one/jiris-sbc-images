"""Exercise the PowerTOP guard without root, namespaces, or host sysfs writes.

Only temporary copies of fixed executable paths are relocated. Production
helpers do not accept command overrides through arguments or the environment.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
HELPERS = PROJECT / "overlay/usr/local/sbin"
CPU = "/sys/devices/system/cpu"


class PowerTOPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rpi4-powertop-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "commands.jsonl"
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.mock(
            "id",
            "assert args == ['-u']\n"
            "print(os.environ.get('TEST_UID', '0'))\n",
        )
        self.mock(
            "unshare",
            "assert args[:4] == ['--mount', '--propagation', 'private', '--']\n"
            "if os.environ.get('TEST_FAIL') == 'unshare':\n"
            "    sys.exit(21)\n"
            "os.execv(args[4], args[4:])\n",
        )
        self.mock(
            "mount",
            f"assert args in (['--bind', {CPU!r}, {CPU!r}], "
            f"['-o', 'remount,bind,ro', {CPU!r}])\n"
            "stage = 'bind' if args[0] == '--bind' else 'remount'\n"
            "if os.environ.get('TEST_FAIL') == stage:\n"
            "    sys.exit(22 if stage == 'bind' else 23)\n",
        )
        self.mock("powertop", "sys.exit(int(os.environ.get('TEST_POWERTOP_EXIT', '0')))\n")
        self.helper = self.root / "rpi4-powertop"
        source = (HELPERS / self.helper.name).read_text()
        for name in ("id", "unshare", "mount", "powertop"):
            source = source.replace(f"/usr/bin/{name}", str(self.commands / name))
        self.helper.write_text(source)
        self.helper.chmod(0o755)
        self.dispatcher = self.root / "powertop"
        self.dispatcher.write_text(
            (HELPERS / self.dispatcher.name).read_text().replace(
                "/usr/local/sbin/rpi4-powertop", str(self.helper)
            )
        )
        self.dispatcher.chmod(0o755)
        self.environment = {**os.environ, "TEST_LOG": str(self.log)}
        for name in ("TEST_UID", "TEST_FAIL", "TEST_POWERTOP_EXIT"):
            self.environment.pop(name, None)

    def mock(self, name, body):
        path = self.commands / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['TEST_LOG'], 'a') as output:\n"
            f"    output.write(json.dumps([{name!r}, args]) + '\\n')\n"
            + body
        )
        path.chmod(0o755)

    def run_guard(self, *args, dispatcher=False, **environment):
        return subprocess.run(
            [str(self.dispatcher if dispatcher else self.helper), *args],
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_non_root_stops_before_namespace_or_powertop(self):
        result = self.run_guard("--auto-tune-dump", TEST_UID="1000")
        self.assertEqual(result.returncode, 1)
        self.assertIn("as root", result.stderr)
        self.assertEqual(self.calls(), [["id", ["-u"]]])

    def test_setup_failures_never_execute_powertop(self):
        for stage, status, names in (
            ("unshare", 21, ["id", "unshare"]),
            ("bind", 22, ["id", "unshare", "mount"]),
            ("remount", 23, ["id", "unshare", "mount", "mount"]),
        ):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                result = self.run_guard("--auto-tune-dump", TEST_FAIL=stage)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual([call[0] for call in self.calls()], names)

    def test_protection_precedes_argument_forwarding_and_exit_status(self):
        arguments = [
            "--html=report with spaces.html",
            "--time=2",
            "",
            "literal 'quotes' \"and doubles\"",
            "line one\nline two",
            f"$(touch {self.root / 'must-not-exist'})",
        ]
        result = self.run_guard(*arguments, TEST_POWERTOP_EXIT="19")
        self.assertEqual(result.returncode, 19, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ["id", "unshare", "mount", "mount", "powertop"])
        self.assertEqual(calls[2], ["mount", ["--bind", CPU, CPU]])
        self.assertEqual(calls[3], ["mount", ["-o", "remount,bind,ro", CPU]])
        self.assertEqual(calls[4], ["powertop", arguments])
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_dispatcher_preserves_arguments_and_requires_the_same_guard(self):
        result = self.run_guard("--auto-tune-dump", "--time=1", dispatcher=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], ["powertop", ["--auto-tune-dump", "--time=1"]])
        self.log.unlink()
        result = self.run_guard("--auto-tune-dump", dispatcher=True, TEST_FAIL="remount")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn("powertop", [call[0] for call in self.calls()])

    def test_caller_path_cannot_replace_guard_commands(self):
        hostile = self.root / "caller-bin"
        hostile.mkdir()
        for name in ("id", "unshare", "mount", "powertop", "rpi4-powertop"):
            path = hostile / name
            path.write_text("#!/bin/sh\nexit 99\n")
            path.chmod(0o755)
        result = self.run_guard("--version", dispatcher=True, PATH=str(hostile))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], ["powertop", ["--version"]])


if __name__ == "__main__":
    unittest.main()
