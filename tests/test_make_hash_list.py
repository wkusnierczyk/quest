import hashlib
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from quest.safety import parse_hash_file

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_hash_list.py"
spec = importlib.util.spec_from_file_location("make_hash_list", SCRIPT)
make_hash_list = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_hash_list)

ZIP_BYTES = b"not really a zip, but only its hash matters for a sideload-only release"
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()


class MakeHashListTests(unittest.TestCase):
    """v85 is sideloaded but never unpacked, so these run without downloads or payload-dumper-go."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.zip = self.root / "firmware_v85.zip"
        self.zip.write_bytes(ZIP_BYTES)
        self.out = self.root / "hash" / "Eureka" / "v85.txt"
        patcher = mock.patch.object(make_hash_list, "HASH_SOURCE_ROOT", self.root / "hash")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_script(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = make_hash_list.main(["v85", "--zip", str(self.zip), *args])
        return code, out.getvalue()

    def write_list(self, text):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(text, encoding="utf-8")

    def test_writes_new_list(self):
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(parse_hash_file(self.out.read_text(encoding="utf-8")), {"firmware_v85.zip": ZIP_SHA})

    def test_changed_hash_is_not_overwritten_without_force(self):
        original = f'"Algorithm","Hash","File"\n"SHA256","{"A" * 64}","firmware_v85.zip"\n'
        self.write_list(original)
        code, output = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("CHANGED", output)
        self.assertEqual(self.out.read_text(encoding="utf-8"), original)
        code, _ = self.run_script("--force")
        self.assertEqual(code, 0)
        self.assertEqual(parse_hash_file(self.out.read_text(encoding="utf-8")), {"firmware_v85.zip": ZIP_SHA})

    def test_malformed_existing_list_is_reported_not_raised(self):
        self.write_list("this is not a hash list\n")
        code, output = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("malformed", output)

    def test_missing_zip_and_unknown_release_are_reported(self):
        self.zip.unlink()
        self.assertEqual(self.run_script()[0], 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(make_hash_list.main(["v999"]), 1)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
