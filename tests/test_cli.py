import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from quest import __version__, cli
from quest.about import package_info
from quest.common import EXIT_USAGE, AlreadyConverted, Refused, data_dir_lock


class CliTests(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    COMMANDS = ("setup", "check", "backup", "download", "verify", "convert", "info", "about", "help")

    def test_every_subcommand_has_help(self):
        for command in self.COMMANDS:
            for flag in ("-h", "-?", "--help"):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    cli.main([command, flag])
                self.assertEqual(ctx.exception.code, 0, (command, flag))

    def run_main(self, argv):
        """Runs cli.main in-process; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def assert_command_list(self, text):
        for command in self.COMMANDS:
            self.assertRegex(text, rf"\n    {command} ", command)
        self.assertNotIn(f"{package_info()['name']} {__version__}", text)  # no startup banner before help

    def test_help_command_and_flags_list_commands(self):
        for argv in (["help"], ["-h"], ["-?"], ["--help"], []):
            with self.subTest(argv=argv):
                code, out, _ = self.run_main(argv)
                self.assertEqual(code, 0)
                self.assert_command_list(out)

    def test_help_for_one_command(self):
        code, out, _ = self.run_main(["help", "check"])
        self.assertEqual(code, 0)
        self.assertIn("usage: quest check", out)
        self.assertIn("--wait SECONDS", out)

    def test_unknown_command_is_a_usage_error_not_exit_2(self):
        for argv in (["help", "bogus"], ["bogus"], ["check", "--bogus"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(argv)
                self.assertEqual(code, EXIT_USAGE)  # 2 means "failed mid-conversion" in this tool
                self.assertIn("error:", err)
        _, _, err = self.run_main(["help", "bogus"])
        self.assertIn("unknown command 'bogus'", err)
        self.assertIn("setup, check, backup", err)

    def test_module_entry_point(self):
        res = subprocess.run([sys.executable, "-m", "quest", "--version"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(__version__, res.stdout)


class AlreadyConvertedExitTests(unittest.TestCase):
    def test_check_and_convert_exit_zero_on_converted_device(self):
        def converted(*args, **kwargs):
            raise AlreadyConverted("Already on user firmware (v85, 51764730125000520). Nothing to convert.")

        def no_dumper(*args, **kwargs):
            raise AssertionError("payload-dumper-go must not be installed for a converted device")

        with mock.patch.object(cli, "inspect_device", converted), \
                mock.patch.object(cli.ToolManager, "ensure", lambda *a, **k: None), \
                mock.patch.object(cli.ToolManager, "ensure_dumper", no_dumper):
            for command in ("check", "convert", "download"):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(cli.main([command, "--wait", "0"]), 0, command)
                self.assertIn("[OK] Already on user firmware (v85", out.getvalue())


class CommandTests(unittest.TestCase):
    """Runs real `python -m quest` commands against an empty, isolated data directory."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cwd = Path(tempfile.mkdtemp())

    def quest(self, *args):
        env = dict(os.environ, QUEST_HOME=str(self.home))
        return subprocess.run([sys.executable, "-m", "quest", *args], capture_output=True, text=True,
                              env=env, cwd=self.cwd, timeout=60, stdin=subprocess.DEVNULL)

    def test_info_reports_isolated_data_dir(self):
        res = self.quest("info")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(str(self.home), res.stdout)
        self.assertIn("not installed", res.stdout)

    def test_verify_without_firmware_fails_cleanly(self):
        res = self.quest("verify", "--target", "v85")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("[MISSING] firmware_v85.zip", res.stdout)
        self.assertIn("Run: quest download", res.stdout)

    def test_check_without_tools_asks_for_setup(self):
        res = self.quest("check", "--wait", "0")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("Run: quest setup", res.stdout)

    def test_commands_leave_working_directory_untouched(self):
        for args in (("info",), ("verify",), ("check", "--wait", "0")):
            self.quest(*args)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_writing_commands_refuse_while_data_dir_is_locked(self):
        with data_dir_lock(self.home):
            for args in (("setup",), ("download", "--target", "v85"), ("convert", "--wait", "0")):
                res = self.quest(*args)
                self.assertEqual(res.returncode, 1, args)
                self.assertIn("Another quest command is using", res.stdout)
            # Read-only commands are not blocked.
            self.assertEqual(self.quest("info").returncode, 0)
        self.assertFalse((self.home / "tools").exists())  # nothing was installed

    @unittest.skipIf(os.name == "nt", "relies on POSIX pipe semantics")
    def test_output_into_a_closed_pipe_exits_quietly(self):
        for args in (["help"], ["--help"], ["about"], ["--about"], ["--version"], ["info"], []):
            with self.subTest(args=args):
                read_end, write_end = os.pipe()
                os.close(read_end)  # nobody reads: every write fails with EPIPE, as after `| head`
                try:
                    env = dict(os.environ, QUEST_HOME=str(self.home))
                    res = subprocess.run([sys.executable, "-m", "quest", *args], stdout=write_end,
                                         stderr=subprocess.PIPE, text=True, env=env, timeout=60)
                finally:
                    os.close(write_end)
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.stderr, "")  # no traceback, no "Exception ignored" warning

    def test_lock_is_exclusive_and_released(self):
        with data_dir_lock(self.home):
            with self.assertRaises(Refused):
                with data_dir_lock(self.home):
                    pass
        with data_dir_lock(self.home):
            pass
