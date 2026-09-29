import io
import tempfile
import unittest
from pathlib import Path

from quest import tool_manager
from quest.common import Console, Refused


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.tm = tool_manager.ToolManager(Console(stream=io.StringIO()))
        self.tm.adb_path = root / "adb"
        self.tm.fastboot_path = root / "fastboot"
        self.tm.dumper_path = root / "payload-dumper-go"
        self.tm.provenance_path = root / "provenance.json"
        for path in (self.tm.adb_path, self.tm.fastboot_path, self.tm.dumper_path):
            path.write_bytes(path.name.encode())

    def record_platform_tools(self, pin=None):
        pin = pin or self.tm._platform_tools_pin()
        self.tm._record("platform-tools", pin, [self.tm.adb_path, self.tm.fastboot_path])

    def test_files_without_provenance_are_not_trusted(self):
        self.assertFalse(self.tm.platform_tools_installed())
        self.assertFalse(self.tm.dumper_installed())

    def test_recorded_install_is_trusted(self):
        self.record_platform_tools()
        self.tm._record("payload-dumper-go", self.tm._dumper_pin(), [self.tm.dumper_path])
        self.assertTrue(self.tm.platform_tools_installed())
        self.assertTrue(self.tm.dumper_installed())

    def test_modified_binary_is_not_trusted(self):
        self.record_platform_tools()
        self.tm.fastboot_path.write_bytes(b"tampered")
        self.assertFalse(self.tm.platform_tools_installed())

    def test_install_from_other_pin_is_not_trusted(self):
        # e.g. an older release, or a payload-dumper-go built for another OS/architecture.
        self.record_platform_tools(pin="0" * 64)
        self.tm._record("payload-dumper-go", "0" * 64, [self.tm.dumper_path])
        self.assertFalse(self.tm.platform_tools_installed())
        self.assertFalse(self.tm.dumper_installed())

    def test_ensure_without_install_refuses_unverified_tools(self):
        with self.assertRaises(Refused):
            self.tm.ensure(install=False, need_drivers=False)


if __name__ == "__main__":
    unittest.main()
