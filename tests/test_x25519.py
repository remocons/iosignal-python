import os
import runpy
import unittest

from _path import iosignal  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))


class X25519Test(unittest.TestCase):
    def test_vectors(self):
        cwd = os.getcwd()
        os.chdir(os.path.join(HERE, ".."))
        try:
            runpy.run_path(os.path.join(HERE, "mpy_x25519.py"))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
