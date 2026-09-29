import io
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import mock

from quest import __version__, cli
from quest.about import package_info, project_urls, render_about, supports_unicode

INFO = {
    "name": "Quest User Firmware Installer",
    "summary": "Convert a userdebug Meta Quest 3 back to user firmware",
    "version": "1.2.3",
    "developer": "mailto:dev@example.com",
    "source": "https://example.com/quest",
    "licence": "MIT https://opensource.org/licenses/MIT",
}


class AboutTests(unittest.TestCase):
    def test_render_matches_the_stanza_format(self):
        self.assertEqual(render_about(INFO).splitlines(), [
            "Quest User Firmware Installer: Convert a userdebug Meta Quest 3 back to user firmware",
            "├─ version:    1.2.3",
            "├─ developer:  mailto:dev@example.com",
            "├─ source:     https://example.com/quest",
            "└─ licence:    MIT https://opensource.org/licenses/MIT",
        ])

    def test_ascii_fallback(self):
        lines = render_about(INFO, unicode=False).splitlines()
        self.assertEqual([line[:2] for line in lines[1:]], ["|-", "|-", "|-", "`-"])
        self.assertTrue(all(line.isascii() for line in lines))

    def test_supports_unicode(self):
        self.assertTrue(supports_unicode(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")))
        self.assertFalse(supports_unicode(io.TextIOWrapper(io.BytesIO(), encoding="ascii")))
        self.assertTrue(supports_unicode(io.StringIO()))

    def test_values_come_from_package_metadata(self):
        info = package_info()
        self.assertEqual(info["name"], "Quest User Firmware Installer")
        self.assertEqual(info["version"], __version__)
        self.assertEqual(info["developer"], "mailto:waclaw.kusnierczyk@gmail.com")
        self.assertEqual(info["source"], "https://github.com/wkusnierczyk/quest")
        self.assertEqual(info["licence"], "MIT https://opensource.org/licenses/MIT")

    def test_project_urls_tolerate_unusual_entries(self):
        entries = ["Source, https://example.com/src", "Issues,https://example.com/i", "no-label-here", ", https://x"]
        self.assertEqual(project_urls(entries), {"Source": "https://example.com/src", "Issues": "https://example.com/i"})

    def test_sparse_metadata_does_not_crash(self):
        meta = Message()
        meta["Project-URL"] = "garbage"
        with mock.patch("quest.about.metadata.metadata", return_value=meta):
            info = package_info()
        self.assertEqual((info["version"], info["source"], info["licence"], info["developer"]),
                         ("unknown", "unknown", "unknown", "unknown"))
        self.assertEqual(info["name"], "Quest User Firmware Installer")

    def test_unknown_distribution_degrades_gracefully(self):
        info = package_info("no-such-distribution-xyz")
        self.assertEqual(info["version"], "unknown")
        self.assertIn("└─ licence:    unknown", render_about(info))

    def test_readme_shows_the_current_stanza(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        block = readme.split("$ quest about\n", 1)[1].split("```", 1)[0].strip()
        self.assertEqual(block, render_about(package_info()), "update the About section in README.md")

    def test_about_command_and_flag_print_only_the_stanza(self):
        expected = render_about(package_info())
        for argv in (["about"], ["--about"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    try:
                        code = cli.main(argv)
                    except SystemExit as e:
                        code = e.code
                self.assertIn(code, (0, None))
                self.assertEqual(out.getvalue().strip(), expected)  # no startup banner, nothing else


if __name__ == "__main__":
    unittest.main()
