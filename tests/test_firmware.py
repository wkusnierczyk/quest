import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from quest.common import Console, Refused
from quest.firmware_manager import FirmwareManager


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FirmwareManagerTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.console = Console(stream=io.StringIO())
        self.fw = FirmwareManager("Eureka", self.console, root / "dumper")
        self.fw.fw_dir = root / "firmware"
        self.fw.hash_dir = root / "hash"
        self.fw.fw_dir.mkdir()
        self.fw.hash_dir.mkdir()
        self.target = self.fw.target("v201")

    def write_hashes(self, version, entries):
        lines = ['"Algorithm","Hash","File"'] + [f'"SHA256","{h.upper()}","{n}"' for n, h in entries.items()]
        (self.fw.hash_dir / f"{version}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def populate_v201(self):
        files = {"boot.img": b"boot", "vendor_boot.img": b"vendor_boot"}
        out = self.fw.extracted_dir(self.target.boot_release)
        out.mkdir()
        for name, data in files.items():
            (out / name).write_bytes(data)
        self.fw.zip_path(self.target.sideload_release).write_bytes(b"zip")
        entries = {name: sha(data) for name, data in files.items()}
        entries["firmware_v201.zip"] = sha(b"zip")
        self.write_hashes("v201", entries)

    def test_verify_target_passes_for_matching_files(self):
        self.populate_v201()
        self.assertTrue(self.fw.verify_target(self.target))

    def test_missing_hash_entry_is_refused_not_passed(self):
        self.populate_v201()
        self.write_hashes("v201", {"boot.img": sha(b"boot")})  # no vendor_boot / zip entries
        with self.assertRaises(Refused):
            self.fw.verify_target(self.target)

    def test_missing_hash_file_is_refused(self):
        with self.assertRaises(Refused):
            self.fw.verify_target(self.target)

    def test_corrupt_file_fails(self):
        self.populate_v201()
        (self.fw.extracted_dir(self.target.boot_release) / "boot.img").write_bytes(b"tampered")
        self.assertFalse(self.fw.verify_target(self.target))

    def test_missing_file_fails(self):
        self.populate_v201()
        (self.fw.extracted_dir(self.target.boot_release) / "vendor_boot.img").unlink()
        self.assertFalse(self.fw.verify_target(self.target))

    def test_cache_is_invalidated_by_modification(self):
        self.populate_v201()
        zip_path = self.fw.zip_path(self.target.sideload_release)
        self.assertTrue(self.fw.verify_file(zip_path, sha(b"zip")))
        zip_path.write_bytes(b"zap")  # same size, new mtime
        stat = zip_path.stat()
        os.utime(zip_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertFalse(self.fw.verify_file(zip_path, sha(b"zip")))

    def test_recheck_ignores_cache(self):
        self.populate_v201()
        self.assertTrue(self.fw.verify_target(self.target))
        boot = self.fw.extracted_dir(self.target.boot_release) / "boot.img"
        stat = boot.stat()
        boot.write_bytes(b"BOOT")  # same size
        os.utime(boot, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # and same mtime: cache would be fooled
        self.assertTrue(self.fw.verify_file(boot, sha(b"boot"), quiet=True))
        self.assertFalse(self.fw.verify_file(boot, sha(b"boot"), use_cache=False, quiet=True))

    def test_empty_expected_never_passes(self):
        self.populate_v201()
        self.assertFalse(self.fw.verify_file(self.fw.zip_path(self.target.sideload_release), None))
        self.assertFalse(self.fw.verify_file(self.fw.zip_path(self.target.sideload_release), ""))


if __name__ == "__main__":
    unittest.main()
