"""Puts src/ on sys.path so tests run without installing the package."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import iosignal  # noqa: E402,F401
