# The MicroPython install list (mip package.json) must hold the board modules that tools/build_web_pkg.py builds.
import json
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
# package.json is at the root of the public repository, and in public/ in the development repository.
PACKAGE = next((p for p in (os.path.join(ROOT, "package.json"), os.path.join(ROOT, "public", "package.json"))
                if os.path.exists(p)), None)


@unittest.skipIf(PACKAGE is None, "no package.json")
class MipPackageTest(unittest.TestCase):
    def test_board_modules(self):
        with open(os.path.join(ROOT, "tools", "build_web_pkg.py"), encoding="utf-8") as f:
            modules = json.loads(re.search(r"^MODULES = (\[.*\])$", f.read(), re.M).group(1).replace("'", '"'))
        with open(PACKAGE, encoding="utf-8") as f:
            pkg = json.load(f)
        names = ["__init__", *modules]
        self.assertEqual([[f"iosignal/{m}.py", f"github:remocons/iosignal-python/src/iosignal/{m}.py"]
                          for m in names], pkg["urls"])
        for m in names:
            self.assertTrue(os.path.exists(os.path.join(ROOT, "src", "iosignal", m + ".py")), m)


class VersionTest(unittest.TestCase):
    def test_one_version(self):
        # The public pyproject is in public/ here and at the root of the public repository.
        path = next(p for p in (os.path.join(ROOT, "public", "pyproject.toml"), os.path.join(ROOT, "pyproject.toml"))
                    if os.path.exists(p))
        with open(path, encoding="utf-8") as f:
            version = re.search(r'^version = "([^"]+)"$', f.read(), re.M).group(1)
        import sys
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from iosignal import __version__
        self.assertEqual(version, __version__)
        if PACKAGE:
            with open(PACKAGE, encoding="utf-8") as f:
                self.assertEqual(version, json.load(f)["version"])
