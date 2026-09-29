import io
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from quest import device_manager
from quest.common import CmdResult, Console


def make_dm():
    dm = device_manager.DeviceManager("adb", "fastboot", Console(stream=io.StringIO()), serial="SERIAL1")
    return dm


class GetvarTests(unittest.TestCase):
    def test_value_only_when_command_succeeded(self):
        dm = make_dm()
        dm.fastboot = lambda *a, **k: CmdResult(["x"], 0, "", "unlocked: yes\n")
        self.assertEqual(dm.fastboot_getvar("unlocked"), "yes")
        dm.fastboot = lambda *a, **k: CmdResult(["x"], None, "", "unlocked: yes\n", timed_out=True)
        self.assertIsNone(dm.fastboot_getvar("unlocked"))
        dm.fastboot = lambda *a, **k: CmdResult(["x"], 1, "", "unlocked: yes\n")
        self.assertIsNone(dm.fastboot_getvar("unlocked"))


class WaitForSideloadTests(unittest.TestCase):
    def setUp(self):
        self.orig_sleep = device_manager.time.sleep
        device_manager.time.sleep = lambda _: None

    def tearDown(self):
        device_manager.time.sleep = self.orig_sleep

    def wait(self, device_lists, timeout=0.05):
        dm = make_dm()
        lists = iter(device_lists)
        last = [[]]

        def adb_devices():
            last[0] = next(lists, last[0])
            return last[0]

        dm.adb_devices = adb_devices
        return dm.wait_for_sideload(timeout)

    def test_pinned_serial(self):
        self.assertEqual(self.wait([[], [("OTHER", "device"), ("SERIAL1", "sideload")]]), "SERIAL1")

    def test_single_device_with_new_serial_accepted(self):
        self.assertEqual(self.wait([[("RECOVERY", "sideload")]]), "RECOVERY")

    def test_other_device_never_accepted_when_several_connected(self):
        self.assertIsNone(self.wait([[("OTHER", "sideload"), ("THIRD", "device")]]))
        self.assertIsNone(self.wait([[("OTHER", "sideload"), ("THIRD", "sideload")]]))


@unittest.skipIf(os.name == "nt", "uses a POSIX shell script as a fake adb")
class StreamToFileTests(unittest.TestCase):
    def fake_adb(self, body):
        path = Path(tempfile.mkdtemp()) / "adb"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        dm = make_dm()
        dm.adb_path = path
        return dm, path.parent / "out.img"

    def test_streams_and_hashes(self):
        dm, out = self.fake_adb("printf 'hello'")
        written, digest, clean = dm.stream_to_file("cat x", out, 5, "x")
        self.assertEqual((written, clean, out.read_bytes()), (5, True, b"hello"))
        self.assertEqual(digest, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_stalled_stream_is_killed(self):
        dm, out = self.fake_adb("printf 'partial'; exec sleep 30")
        start = time.monotonic()
        written, _, clean = dm.stream_to_file("cat x", out, 100, "x", stall_timeout=1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(written, 7)
        self.assertFalse(clean)


if __name__ == "__main__":
    unittest.main()
