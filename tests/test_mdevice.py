import os
import runpy
import unittest

from _path import iosignal  # noqa: F401
from iosignal import devices, mdevice

HERE = os.path.dirname(os.path.abspath(__file__))


class MDeviceTest(unittest.TestCase):
    def test_board_script(self):
        cwd = os.getcwd()
        os.chdir(os.path.join(HERE, ".."))
        try:
            runpy.run_path(os.path.join(HERE, "mpy_mdevice.py"))
        finally:
            os.chdir(cwd)

    def test_same_as_apply_command(self):
        starts = ["on=0", "on=1;level=50", "r1=0;r2=1;mode=a", "s1=1;s2=1", "", "level=5"]
        cmds = ["on", "off", "toggle", "all=1", "all=0", "all=x", "level=70", "mode=b", "beep", "x=1;y",
                "9a=1", "  on ", "color=#ff0000", "_k=v", "a-b=1"]
        for start in starts:
            for cmd in cmds:
                st = mdevice.State(start)
                changed = st.apply(cmd)
                want, want_changed = devices.apply_command(devices.parse_state(start), cmd)
                self.assertEqual(devices.parse_state(st.text()), devices.parse_state(devices.format_state(want)),
                                 (start, cmd))
                self.assertEqual(changed, want_changed, (start, cmd))

    def test_ui_parsed_by_the_rules(self):
        ui = devices.parse_ui(mdevice.format_ui("dimmer.1", "on,off", False, True))
        self.assertEqual((ui.type, ui.ui, ui.has_view, ui.has_panel), ("dimmer.1", "on,off", True, False))
        self.assertTrue(devices.is_extended_ui(mdevice.format_ui()))


if __name__ == "__main__":
    unittest.main()
