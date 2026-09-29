import hashlib
import io
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quest import safety, workflow
from quest.common import AlreadyConverted, CmdResult, Console, ConversionFailed, Refused, shell_quote
from quest.firmware_manager import TARGETS, PreparedFirmware

PROPS = {
    "ro.hardware": "eureka",
    "ro.build.type": "userdebug",
    "ro.boot.flash.locked": "0",
    "ro.boot.verifiedbootstate": "orange",
    "ro.build.version.incremental": str(safety.V72_BASELINE_INCREMENTAL),
    "ro.serialno": "SERIAL1",
    "ro.vendor.product.model_extended": "Eureka PVT2",
}


def ok(output=""):
    return CmdResult(["x"], 0, output)


def fail(output="error"):
    return CmdResult(["x"], 1, output)


class FakeDevice:
    adb_path = "adb"
    fastboot_path = "fastboot"

    def __init__(self, props=None, **overrides):
        self.serial = None
        self.props = dict(PROPS, **(props or {}))
        self.calls = []
        self.results = {
            "reboot_bootloader": ok(), "wait_for_fastboot": True, "fastboot_getvar": "yes",
            "flash": {}, "oem_reboot_sideload": ok(), "wait_for_sideload": "SERIAL1", "sideload": ok(),
            "try_root": True, "i2c": set(),
        }
        self.results.update(overrides)

    def select_device(self, wait_seconds=0):
        self.serial = "SERIAL1"
        return self.serial

    def try_root(self):
        return self.results["try_root"]

    def get_props(self):
        return dict(self.props)

    def read_i2c_bus4(self):
        return self.results["i2c"], ok()

    def get_slot_suffix(self):
        return "_b"

    def reboot_bootloader(self):
        self.calls.append("reboot_bootloader")
        return self.results["reboot_bootloader"]

    def wait_for_fastboot(self, timeout):
        return self.results["wait_for_fastboot"]

    def fastboot_getvar(self, name):
        return self.results["fastboot_getvar"]

    def flash(self, partition, image):
        self.calls.append(f"flash {partition}")
        return self.results["flash"].get(partition, ok())

    def oem_reboot_sideload(self):
        self.calls.append("oem_reboot_sideload")
        return self.results["oem_reboot_sideload"]

    def wait_for_sideload(self, timeout):
        return self.results["wait_for_sideload"]

    def sideload(self, zip_path, serial):
        self.calls.append("sideload")
        return self.results["sideload"]


class FakeFirmware:
    def __init__(self, images_ok=True, zip_ok=True):
        self.images_ok = images_ok
        self.zip_ok = zip_ok
        self.verify_calls = []

    def recheck_boot_images(self, prepared):
        return self.images_ok

    def verify_file(self, path, expected, **kwargs):
        self.verify_calls.append(kwargs)
        return self.zip_ok


def prepared():
    boot = {"boot": Path("/fw/extracted_v72/boot.img"), "vendor_boot": Path("/fw/extracted_v72/vendor_boot.img")}
    zip_path = Path("/fw/firmware_v85.zip")
    expected = {p: "0" * 64 for p in list(boot.values()) + [zip_path]}
    return PreparedFirmware(TARGETS["Eureka"]["v85"], boot, zip_path, expected)


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.console = Console(stream=io.StringIO())

    def test_compatible_device(self):
        info = workflow.inspect_device(FakeDevice(), self.console)
        self.assertEqual((info.serial, info.target, info.device_name), ("SERIAL1", "v85", "Eureka"))

    def test_converted_device_reports_already_converted_without_root_attempt(self):
        dev = FakeDevice({"ro.build.type": "user", "ro.build.version.incremental": "51764730125000520"})
        dev.try_root = lambda: self.fail("adb root must not be attempted on a user build")
        with self.assertRaises(AlreadyConverted) as ctx:
            workflow.inspect_device(dev, self.console)
        self.assertIn("v85", str(ctx.exception))
        self.assertNotIn("adb root", self.console.stream.getvalue())

    def test_locked_stock_headset_on_target_firmware_is_refused_not_converted(self):
        locked = {"ro.build.type": "user", "ro.build.version.incremental": "51764730125000520",
                  "ro.boot.flash.locked": "1", "ro.boot.verifiedbootstate": "green"}
        with self.assertRaises(Refused) as ctx:
            workflow.inspect_device(FakeDevice(locked), self.console)
        self.assertIn("Bootloader is locked", str(ctx.exception))

    def test_user_build_on_other_incremental_still_refused(self):
        with self.assertRaises(Refused):
            workflow.inspect_device(FakeDevice({"ro.build.type": "user", "ro.build.version.incremental": "5"}),
                                    self.console)

    def test_locked_device_refused(self):
        with self.assertRaises(Refused):
            workflow.inspect_device(FakeDevice({"ro.boot.flash.locked": "1"}), self.console)

    def test_pvt11_requires_hal_pass(self):
        pvt11 = {"ro.vendor.product.model_extended": "Eureka PVT1.1"}
        with self.assertRaises(Refused):
            workflow.inspect_device(FakeDevice(pvt11, i2c={0x10}), self.console)
        info = workflow.inspect_device(FakeDevice(pvt11, i2c={0x10, 0x20}), self.console)
        self.assertEqual(info.target, "v85")

    def test_pvt11_failed_i2c_read_is_refused_even_with_safe_looking_output(self):
        dev = FakeDevice({"ro.vendor.product.model_extended": "Eureka PVT1.1"}, i2c={0x10, 0x20})
        dev.read_i2c_bus4 = lambda: ({0x10, 0x20}, CmdResult(["x"], None, "10: 10 ...", timed_out=True))
        with self.assertRaises(Refused):
            workflow.inspect_device(dev, self.console)


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.console = Console(stream=io.StringIO())

    def run_convert(self, dev, fw=None, confirm=True):
        info = workflow.inspect_device(dev, self.console)
        return workflow.convert(dev, fw or FakeFirmware(), self.console, info, prepared(), confirm=lambda: confirm)

    def test_happy_path_order(self):
        dev = FakeDevice()
        self.run_convert(dev)
        self.assertEqual(dev.calls, ["reboot_bootloader", "flash boot_b", "flash vendor_boot_b",
                                     "oem_reboot_sideload", "sideload"])

    def test_declined_confirmation_touches_nothing(self):
        dev = FakeDevice()
        with self.assertRaises(Refused):
            self.run_convert(dev, confirm=False)
        self.assertEqual(dev.calls, [])

    def test_corrupt_images_touch_nothing(self):
        for fw in (FakeFirmware(images_ok=False), FakeFirmware(zip_ok=False)):
            dev = FakeDevice()
            with self.assertRaises(Refused):
                self.run_convert(dev, fw=fw)
            self.assertEqual(dev.calls, [])

    def test_state_change_between_check_and_convert_refused(self):
        dev = FakeDevice()
        info = workflow.inspect_device(dev, self.console)
        dev.props["ro.serialno"] = "OTHER"
        with self.assertRaises(Refused):
            workflow.convert(dev, FakeFirmware(), self.console, info, prepared(), confirm=lambda: True)
        self.assertEqual(dev.calls, [])

    def test_boot_flash_failure_stops_before_vendor_boot(self):
        dev = FakeDevice(flash={"boot_b": fail()})
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertEqual(dev.calls, ["reboot_bootloader", "flash boot_b"])
        self.assertIn("vendor_boot_b", ctx.exception.recovery)
        self.assertIn("boot_b", ctx.exception.recovery)

    def test_vendor_boot_failure_does_not_reboot(self):
        dev = FakeDevice(flash={"vendor_boot_b": fail()})
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertNotIn("oem_reboot_sideload", dev.calls)
        self.assertIn("Do NOT reboot", ctx.exception.recovery)
        self.assertNotIn("flash boot_b ", ctx.exception.recovery.replace("\n", " ") + " ")

    def test_locked_in_fastboot_stops(self):
        dev = FakeDevice(fastboot_getvar="no")
        with self.assertRaises(ConversionFailed):
            self.run_convert(dev)
        self.assertEqual(dev.calls, ["reboot_bootloader"])

    def test_unknown_lock_state_stops_before_flashing(self):
        dev = FakeDevice(fastboot_getvar=None)
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertEqual(dev.calls, ["reboot_bootloader"])
        self.assertIn("Nothing was written", ctx.exception.recovery)
        self.assertNotIn("flash", ctx.exception.recovery)

    def test_sideload_zip_is_fully_rehashed_before_use(self):
        fw = FakeFirmware()
        self.run_convert(FakeDevice(), fw=fw)
        self.assertEqual(fw.verify_calls, [{"use_cache": False}])

    def test_fastboot_timeout(self):
        dev = FakeDevice(wait_for_fastboot=False)
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertIn("Nothing was written", ctx.exception.recovery)

    def test_sideload_wait_timeout(self):
        dev = FakeDevice(wait_for_sideload=None)
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertNotIn("sideload", dev.calls)
        self.assertIn("Apply update from ADB", ctx.exception.recovery)

    def test_sideload_failure(self):
        dev = FakeDevice(sideload=fail("adb: sideload connection failed: closed"))
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertIn("sideload", ctx.exception.recovery)

    def test_sideload_known_benign_error(self):
        dev = FakeDevice(sideload=fail("serving: 'firmware_v85.zip'  (~47%)    adb: failed to read command: Success"))
        self.run_convert(dev)  # no exception

    def test_interrupt_mid_flash_reports_recovery(self):
        dev = FakeDevice()

        def interrupted(partition, image):
            dev.calls.append(f"flash {partition}")
            raise KeyboardInterrupt

        dev.flash = interrupted
        with self.assertRaises(ConversionFailed) as ctx:
            self.run_convert(dev)
        self.assertIn("boot_b", ctx.exception.recovery)


class ClosedOutputDuringConversionTests(unittest.TestCase):
    def test_closing_the_output_between_flash_steps_does_not_stop_the_conversion(self):
        from test_console import ClosedPipe
        dev = FakeDevice()
        stream = ClosedPipe(close_on="Flashed boot_b")  # the reader goes away right after the first flash
        console = Console(stream=stream)
        info = workflow.inspect_device(dev, console)
        workflow.convert(dev, FakeFirmware(), console, info, prepared(), confirm=lambda: True)
        self.assertTrue(stream.reader_gone)
        self.assertEqual(dev.calls, ["reboot_bootloader", "flash boot_b", "flash vendor_boot_b",
                                     "oem_reboot_sideload", "sideload"])
        self.assertNotIn("Flashed vendor_boot_b", stream.getvalue())  # later output was dropped


class MidConversionErrorTests(unittest.TestCase):
    def test_adb_error_after_flashing_is_a_conversion_failure(self):
        dev = FakeDevice()

        def adb_gone(timeout):
            raise Refused("could not run adb")

        dev.wait_for_sideload = adb_gone
        console = Console(stream=io.StringIO())
        info = workflow.inspect_device(dev, console)
        with self.assertRaises(ConversionFailed):
            workflow.convert(dev, FakeFirmware(), console, info, prepared(), confirm=lambda: True)


PARTITIONS = {"boot_a": b"B" * 4096, "misc": b"M" * 1024, "userdata": b"U" * 10}
MAPPER = {"system_a": b"S" * 2048, "system_b": b"s" * 2048, "control": b""}


class FakeBackupDevice(FakeDevice):
    def __init__(self, short_read=None, device_hash_wrong=False):
        super().__init__()
        self.console = Console(stream=io.StringIO())
        self.short_read = short_read
        self.device_hash_wrong = device_hash_wrong

    def _data(self, path):
        directory, name = path.rsplit("/", 1)
        return (MAPPER if directory == workflow.MAPPER_DIR else PARTITIONS)[name]

    def shell(self, command, timeout=60):
        if command.startswith("ls "):
            names = MAPPER if command.endswith("mapper") else PARTITIONS
            return ok("\n".join(list(names) + ["bad;name"]))
        if command.startswith("blockdev --getsize64 "):
            return ok(str(len(self._data(command.split()[-1]))))
        if command.startswith("sha256sum "):
            path = command.split()[-1]
            digest = "0" * 64 if self.device_hash_wrong else hashlib.sha256(self._data(path)).hexdigest()
            return ok(f"{digest}  {path}")
        return fail()

    def stream_to_file(self, shell_command, dest, total, label):
        path = shell_command.split()[1]
        data = self._data(path)
        if path.endswith(self.short_read or "\0"):
            data = data[:-1]
        Path(dest).write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest(), True


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_backup_writes_images_and_manifest(self):
        dev = FakeBackupDevice()
        dest = workflow.backup(dev, dev.console, verify_on_device=True, dest_root=self.root)
        self.assertEqual((dest / "by-name" / "boot_a.img").read_bytes(), PARTITIONS["boot_a"])
        self.assertTrue((dest / "mapper" / "system_b.img").exists())
        self.assertFalse((dest / "by-name" / "userdata.img").exists())
        self.assertFalse((dest / "mapper" / "control.img").exists())
        manifest = safety.parse_hash_file((dest / "SHA256SUMS.csv").read_text(encoding="utf-8"))
        self.assertEqual(manifest["by-name/misc.img"], hashlib.sha256(PARTITIONS["misc"]).hexdigest())
        self.assertEqual(len(manifest), 4)

    def test_short_read_fails(self):
        dev = FakeBackupDevice(short_read="/misc")
        with self.assertRaises(Refused) as ctx:
            workflow.backup(dev, dev.console, dest_root=self.root)
        self.assertIn("by-name/misc", str(ctx.exception))

    def test_on_device_hash_mismatch_fails(self):
        dev = FakeBackupDevice(device_hash_wrong=True)
        with self.assertRaises(Refused):
            workflow.backup(dev, dev.console, verify_on_device=True, dest_root=self.root)

    def test_back_to_back_backups_never_share_a_directory(self):
        dev = FakeBackupDevice()
        first = workflow.backup(dev, dev.console, dest_root=self.root)
        second = workflow.backup(dev, dev.console, dest_root=self.root)
        self.assertNotEqual(first, second)
        self.assertTrue((first / "SHA256SUMS.csv").exists())
        self.assertTrue((second / "SHA256SUMS.csv").exists())

    def test_requires_root(self):
        dev = FakeBackupDevice()
        dev.results["try_root"] = False
        with self.assertRaises(Refused):
            workflow.backup(dev, dev.console, dest_root=self.root)


class RecoveryCommandTests(unittest.TestCase):
    """Recovery commands are printed when the headset is mid-conversion; they must run as printed."""

    TOOLS = "/Users/me/Library/Application Support/quest/tools/platform-tools"
    FW = "/Users/me/Library/Application Support/quest/firmware/Eureka"

    TOOL_PATHS = (f"{TOOLS}/adb", f"{TOOLS}/fastboot")

    def fail_at(self, windows=False, **overrides):
        dev = FakeDevice(**overrides)
        dev.adb_path = f"{self.TOOLS}/adb"
        dev.fastboot_path = f"{self.TOOLS}/fastboot"
        boot = {"boot": Path(f"{self.FW}/extracted_v72/boot.img"),
                "vendor_boot": Path(f"{self.FW}/extracted_v72/vendor_boot.img")}
        zip_path = Path(f"{self.FW}/firmware_v85.zip")
        prep = PreparedFirmware(TARGETS["Eureka"]["v85"], boot, zip_path,
                                {p: "0" * 64 for p in [*boot.values(), zip_path]})
        console = Console(stream=io.StringIO())
        info = workflow.inspect_device(dev, console)
        # The recovery text is rendered for one platform's shell; test both, on any host.
        with mock.patch("quest.common.IS_WINDOWS", windows), self.assertRaises(ConversionFailed) as ctx:
            workflow.convert(dev, FakeFirmware(), console, info, prep, confirm=lambda: True)
        return ctx.exception.recovery

    def commands(self, recovery, windows=False):
        """The recovery lines that parse, in the given shell's syntax, into a call of one of the tools."""
        split = split_powershell if windows else shlex.split
        argvs = []
        for line in (line.strip() for line in recovery.splitlines()):
            try:
                argv = split(line)
            except ValueError:  # prose, not a command
                continue
            if argv and argv[0] in self.TOOL_PATHS:
                argvs.append(argv)
        return argvs

    def test_every_stage_prints_runnable_commands(self):
        stages = {
            "boot flash": {"flash": {"boot_b": fail()}},
            "vendor_boot flash": {"flash": {"vendor_boot_b": fail()}},
            "reboot-sideload": {"oem_reboot_sideload": fail()},
            "sideload wait": {"wait_for_sideload": None},
            "sideload": {"sideload": fail("adb: sideload connection failed")},
            "fastboot wait": {"wait_for_fastboot": False},
        }
        for windows in (False, True):
            for stage, overrides in stages.items():
                with self.subTest(stage, windows=windows):
                    recovery = self.fail_at(windows, **overrides)
                    argvs = self.commands(recovery, windows)
                    self.assertTrue(argvs, f"no commands printed: {stage}")
                    for argv in argvs:
                        self.assertEqual(argv[1:3], ["-s", "SERIAL1"])
                    # Every line that names a tool is a complete, parseable command.
                    tool_lines = [line for line in recovery.splitlines() if self.TOOLS in line]
                    self.assertEqual(len(tool_lines), len(argvs))

    def test_paths_with_spaces_round_trip(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                self.check_round_trip(windows)

    def check_round_trip(self, windows):
        argvs = self.commands(self.fail_at(windows, flash={"vendor_boot_b": fail()}), windows)
        self.assertIn([f"{self.TOOLS}/fastboot", "-s", "SERIAL1", "flash", "vendor_boot_b",
                       f"{self.FW}/extracted_v72/vendor_boot.img"], argvs)
        self.assertIn([f"{self.TOOLS}/adb", "-s", "SERIAL1", "sideload", f"{self.FW}/firmware_v85.zip"], argvs)
        self.assertNotIn(["flash", "boot_b"], [a[3:5] for a in argvs])  # already flashed; not repeated


class ShellQuoteTests(unittest.TestCase):
    def test_posix(self):
        argv = ["/a b/fastboot", "-s", "X", "flash", "boot_a", "/p q/it's.img"]
        self.assertEqual(shlex.split(shell_quote(argv, windows=False)), argv)

    def test_platform_default_is_decided_at_call_time(self):
        with mock.patch("quest.common.IS_WINDOWS", True):
            self.assertTrue(shell_quote(["a b"]).startswith("& "))
        with mock.patch("quest.common.IS_WINDOWS", False):
            self.assertEqual(shell_quote(["a b"]), "'a b'")

    def test_powershell_round_trip(self):
        argv = [r"C:\Users\A B\fastboot.exe", "-s", "X", "flash", "boot_a", r"C:\it's here\f.img"]
        self.assertEqual(split_powershell(shell_quote(argv, windows=True)), argv)

    def test_powershell(self):
        line = shell_quote([r"C:\Users\A B\fastboot.exe", "-s", "X", r"C:\it's\f.img"], windows=True)
        self.assertEqual(line, r"& 'C:\Users\A B\fastboot.exe' -s X 'C:\it''s\f.img'")


def split_powershell(line):
    """Parses the PowerShell subset shell_quote() emits: '& ' then bare or single-quoted ('' escapes) tokens."""
    if not line.startswith("& "):
        raise ValueError("not a PowerShell command line")
    return [m.group(1).replace("''", "'") if m.group(1) is not None else m.group(2)
            for m in re.finditer(r"'((?:[^']|'')*)'|(\S+)", line[2:])]


class ClassifySideloadTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(workflow.classify_sideload(ok()), "ok")
        self.assertEqual(workflow.classify_sideload(fail("Total xfer: 1.00x")), "probably-ok")
        self.assertEqual(workflow.classify_sideload(fail("Total xfer: 1.02x")), "probably-ok")
        self.assertEqual(workflow.classify_sideload(fail("Total xfer: 0.00x")), "failed")
        self.assertEqual(workflow.classify_sideload(fail("Total xfer: 0.47x")), "failed")
        partial = "adb: failed to read command: Success\nTotal xfer: 0.47x"
        self.assertEqual(workflow.classify_sideload(fail(partial)), "failed")
        self.assertEqual(workflow.classify_sideload(fail("adb: failed to read command: Success")), "probably-ok")
        self.assertEqual(workflow.classify_sideload(fail("adb: failed to read command: No error")), "failed")
        self.assertEqual(workflow.classify_sideload(CmdResult(["x"], None, "Total xfer", timed_out=True)), "failed")


if __name__ == "__main__":
    unittest.main()
