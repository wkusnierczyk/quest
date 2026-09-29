import unittest

from quest import safety
from quest.common import HASH_ROOT
from quest.firmware_manager import TARGETS

I2C_GRID = """\
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- 08 -- -- -- -- -- -- --
10: 10 -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: UU -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
40: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
50: -- -- -- -- -- -- 56 -- -- -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- --
"""

COMPATIBLE_PROPS = {
    "ro.hardware": "eureka",
    "ro.build.type": "userdebug",
    "ro.boot.flash.locked": "0",
    "ro.boot.verifiedbootstate": "orange",
    "ro.build.version.incremental": str(safety.V72_BASELINE_INCREMENTAL),
}


class ParserTests(unittest.TestCase):
    def test_i2c_uses_column_positions(self):
        # Row 00 starts with blank (unscanned) cells; 0x08 must not be shifted to 0x05.
        self.assertEqual(safety.parse_i2c_addresses(I2C_GRID), {0x08, 0x10, 0x20, 0x56})

    def test_i2c_ignores_noise(self):
        noisy = "ls: /sys/bus/i2c/devices/4-0056/driver: No such file or directory\nas5510\n" + I2C_GRID
        self.assertEqual(safety.parse_i2c_addresses(noisy), {0x08, 0x10, 0x20, 0x56})
        self.assertEqual(safety.parse_i2c_addresses("i2cdetect: not found"), set())

    def test_getprop(self):
        text = "[ro.hardware]: [eureka]\n[ro.empty]: []\n[ro.build.type]: [userdebug]\n"
        self.assertEqual(safety.parse_getprop(text),
                         {"ro.hardware": "eureka", "ro.empty": "", "ro.build.type": "userdebug"})

    def test_device_list(self):
        text = ("* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
                "List of devices attached\n2G0YC1ZF8B0XYZ\tdevice usb:1-1 product:eureka\nABC\tunauthorized\n\n")
        self.assertEqual(safety.parse_device_list(text), [("2G0YC1ZF8B0XYZ", "device"), ("ABC", "unauthorized")])
        self.assertEqual(safety.parse_device_list("2G0YC1ZF8B0XYZ\tfastboot\n"), [("2G0YC1ZF8B0XYZ", "fastboot")])

    def test_fastboot_getvar(self):
        out = "unlocked: yes\nFinished. Total time: 0.001s\n"
        self.assertEqual(safety.parse_fastboot_getvar(out, "unlocked"), "yes")
        self.assertEqual(safety.parse_fastboot_getvar("(bootloader) unlocked:no\n", "unlocked"), "no")
        self.assertIsNone(safety.parse_fastboot_getvar("getvar:unlocked FAILED\n", "unlocked"))

    def test_incremental(self):
        self.assertEqual(safety.parse_incremental(" 51780870000000521 "), 51780870000000521)
        for bad in ("", "abc", None, "0", "-5"):
            self.assertIsNone(safety.parse_incremental(bad))

    def test_hash_file_strict(self):
        good = '"Algorithm","Hash","File"\n"SHA256","' + "AB" * 32 + '","boot.img"\n'
        self.assertEqual(safety.parse_hash_file(good), {"boot.img": "ab" * 32})
        for bad in ('"Algo","Hash"\n',
                    '"Algorithm","Hash","File"\n"MD5","' + "ab" * 16 + '","boot.img"\n',
                    '"Algorithm","Hash","File"\n"SHA256","xyz","boot.img"\n',
                    '"Algorithm","Hash","File"\n"SHA256","' + "ab" * 32 + '"\n'):
            with self.assertRaises(ValueError):
                safety.parse_hash_file(bad)

    def test_repository_hash_lists_cover_exactly_what_conversion_uses(self):
        for device, targets in TARGETS.items():
            hash_dir = HASH_ROOT / device
            lists = {p.name.removesuffix(".txt"): safety.parse_hash_file(p.read_text(encoding="utf-8"))
                     for p in hash_dir.iterdir() if p.name.endswith(".txt")}
            needed = {}
            for target in targets.values():
                for release in (target.boot_release, target.sideload_release):
                    needed.setdefault(release.version, {f"firmware_{release.version}.zip"})
                needed[target.boot_release.version] |= {"boot.img", "vendor_boot.img"}
            self.assertEqual({v: set(entries) for v, entries in lists.items()}, needed)


class RuleTests(unittest.TestCase):
    def test_match_target(self):
        self.assertEqual(safety.match_target(safety.V72_BASELINE_INCREMENTAL), "v85")
        self.assertEqual(safety.match_target(safety.V72_BASELINE_INCREMENTAL - 1), "v85")
        self.assertIsNone(safety.match_target(safety.V72_BASELINE_INCREMENTAL + 1))
        for incr in safety.V201_INCREMENTALS:
            self.assertEqual(safety.match_target(incr), "v201")
        self.assertIsNone(safety.match_target(None))

    def test_hal_rule(self):
        safe = [{0x10, 0x20}, {0x20, 0x56}, {0x10, 0x20, 0x56}, {0x75}, {0x74}]
        unsafe = [set(), {0x10}, {0x20}, {0x56}, {0x10, 0x56}, {0x75, 0x74}, {0x75, 0x10}, {0x30}]
        for addrs in safe:
            self.assertTrue(safety.hal_is_safe(addrs), addrs)
        for addrs in unsafe:
            self.assertFalse(safety.hal_is_safe(addrs), addrs)

    def test_compatibility(self):
        self.assertEqual(safety.compatibility_problems(COMPATIBLE_PROPS), [])
        cases = {
            "ro.hardware": "hollywood",
            "ro.build.type": "user",
            "ro.boot.flash.locked": "1",
            "ro.boot.verifiedbootstate": "green",
            "ro.build.version.incremental": "garbage",
        }
        for key, value in cases.items():
            props = dict(COMPATIBLE_PROPS, **{key: value})
            self.assertEqual(len(safety.compatibility_problems(props)), 1, key)
        unsupported = dict(COMPATIBLE_PROPS, **{"ro.build.version.incremental": "99999999999999999"})
        self.assertEqual(len(safety.compatibility_problems(unsupported)), 1)
        self.assertGreaterEqual(len(safety.compatibility_problems({})), 4)

    def test_already_converted(self):
        user = dict(COMPATIBLE_PROPS, **{"ro.build.type": "user"})
        for incremental, target in safety.CONVERTED_INCREMENTALS.items():
            props = dict(user, **{"ro.build.version.incremental": str(incremental)})
            self.assertEqual(safety.already_converted(props), target)
            # The same incremental on a userdebug build, or on other hardware, is not "converted".
            self.assertIsNone(safety.already_converted(dict(props, **{"ro.build.type": "userdebug"})))
            self.assertIsNone(safety.already_converted(dict(props, **{"ro.hardware": "hollywood"})))
            # A stock headset with a locked bootloader on the same firmware was never converted.
            self.assertIsNone(safety.already_converted(dict(props, **{"ro.boot.flash.locked": "1"})))
            self.assertIsNone(safety.already_converted(dict(props, **{"ro.boot.verifiedbootstate": "green"})))
        self.assertIsNone(safety.already_converted(dict(user, **{"ro.build.version.incremental": "123"})))
        self.assertIsNone(safety.already_converted(COMPATIBLE_PROPS))

    def test_converted_incrementals_match_sideloaded_firmware(self):
        # Each target's "already converted" incremental is the one in the URL of the firmware it sideloads.
        for name, target in TARGETS["Eureka"].items():
            url_incremental = int(target.sideload_release.url.rsplit("_", 1)[1].removesuffix(".zip"))
            self.assertEqual(safety.CONVERTED_INCREMENTALS.get(url_incremental), name)

    def test_slot_and_partition_names(self):
        self.assertTrue(safety.is_valid_slot_suffix("_a"))
        self.assertTrue(safety.is_valid_slot_suffix("_b"))
        for bad in ("", "a", "_c", "_a\n", "_a;reboot"):
            self.assertFalse(safety.is_valid_slot_suffix(bad), bad)
        self.assertTrue(safety.is_safe_partition_name("vendor_boot_a"))
        for bad in ("..", "a b", "x;rm", "$(id)", ""):
            self.assertFalse(safety.is_safe_partition_name(bad), bad)


if __name__ == "__main__":
    unittest.main()
