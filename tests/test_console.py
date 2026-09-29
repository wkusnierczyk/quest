import io
import tempfile
import unittest
from pathlib import Path

from quest.common import Console, Refused


class ClosedPipe(io.StringIO):
    """
    A stream whose reader goes away, like `quest ... | head`: after `fail_after` writes, or when
    the text being written contains `close_on`. Every write from then on fails with EPIPE.
    """

    def __init__(self, fail_after=None, close_on=None):
        super().__init__()
        self.fail_after = fail_after
        self.close_on = close_on
        self.writes = 0
        self.reader_gone = False

    def write(self, text):
        self.writes += 1
        if (self.fail_after is not None and self.writes > self.fail_after) or (self.close_on and self.close_on in text):
            self.reader_gone = True
        if self.reader_gone:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(text)


class ClosedOutputTests(unittest.TestCase):
    def test_closed_output_does_not_raise_and_the_log_keeps_everything(self):
        log = Path(tempfile.mkdtemp()) / "run.log"
        console = Console(stream=ClosedPipe(fail_after=1), log_path=log)
        console.info("first")
        console.info("second")  # the pipe is closed here
        console.warn("third")
        console.raw("tool output\n")
        console.progress("download", 50, 100)
        console.close()
        self.assertEqual(console.stream.getvalue(), "first\n")
        self.assertEqual(log.read_text(encoding="utf-8"), "first\nsecond\n[WARN] third\ntool output\n")

    def test_no_confirmation_without_a_visible_summary(self):
        console = Console(stream=ClosedPipe(fail_after=0))
        with self.assertRaises(Refused):
            console.confirm_phrase("summary", "CONVERT", input_fn=lambda prompt: "CONVERT")


if __name__ == "__main__":
    unittest.main()
