import unittest
import os
import re
from unittest.mock import patch

from packetsafari_onprem import menu
from packetsafari_onprem.prompts import PromptOption, PromptSession


class MenuPromptTests(unittest.TestCase):
    def test_descriptions_do_not_shift_menu_rows(self):
        class Session(PromptSession):
            size = (18, 64)
            def __init__(self):
                self.keys = iter(("down", "down", "\r"))
                self.frames = []
            def draw(self, lines):
                self.frames.append([re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines])
            def read_key(self, *_): return next(self.keys)
        session = Session()
        options = [PromptOption("health", "Health", "Inspect safely"),
                   PromptOption("upgrade", "Upgrade", "Review release"),
                   PromptOption("back", "Back")]
        self.assertEqual(session.select("Operations", options), "back")
        for frame in session.frames:
            self.assertEqual(len(frame), len(session.frames[0]))
            for option in options:
                self.assertEqual(next(i for i, row in enumerate(frame) if row.endswith(option.label)),
                                 next(i for i, row in enumerate(session.frames[0]) if row.endswith(option.label)))
            self.assertEqual(frame[-1], session.frames[0][-1])
        self.assertIn("Inspect safely", session.frames[0][-2])
        self.assertEqual(session.frames[-1][-2].strip(), "│")

    def test_raw_input_keeps_enter_after_arrow_and_cancels_control_c(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b"\x1b[B\r\x03")
            session = PromptSession()
            session.fd = reader
            self.assertEqual(session.read_key(), "down")
            self.assertEqual(session.read_key(), "\r")
            with self.assertRaises(KeyboardInterrupt):
                session.read_key()
        finally:
            os.close(reader)
            os.close(writer)

    def test_wide_text_is_clipped_to_terminal_cells(self):
        row = PromptSession.clip_row("\x1b[36m◆ " + "界" * 30, 20)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", row), "◆ " + "界" * 8)

    def test_prompt_menu_runs_an_action_then_returns_to_the_same_level(self):
        calls = []

        class Session:
            responses = iter(("0", None))
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def select(self, title, options, **kwargs):
                self.test_case.assertEqual(title, "What do you want to manage?")
                self.test_case.assertEqual(options[0].hint, "Inspect safely")
                self.test_case.assertEqual(kwargs["completed"]()[0][0], "Host")
                self.test_case.assertTrue(callable(kwargs["note"]))
                self.test_case.assertTrue(callable(kwargs["on_idle"]))
                return next(self.responses)

        Session.test_case = self
        ctx = menu.MenuContext("/tmp/runtime", "/opt/packetsafari", "http://127.0.0.1")
        ctx.local_status = {"state": {"deployment": {"mode": "onprem", "installedVersion": "1.2.3"}}}
        items = [menu.MenuItem("Overview", "Inspect safely", action=lambda value: calls.append(value))]
        with patch.object(menu, "PromptSession", Session):
            self.assertTrue(menu._walk_menu(["Operations"], items, ctx, allow_back=False, allow_quit=True))
        self.assertEqual(calls, [ctx])


    def test_prompt_submenu_collapses_navigation_path(self):
        seen = []

        class Session:
            responses = iter(("0", None, None))
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def select(self, title, _options, **kwargs):
                seen.append((title, kwargs["completed"]))
                return next(self.responses)

        ctx = menu.MenuContext("/tmp/runtime", "/opt/packetsafari", "http://127.0.0.1")
        items = [menu.MenuItem("Health", "Open health", submenu_factory=lambda _: [menu.MenuItem("Back", "No action")])]
        with patch.object(menu, "PromptSession", Session):
            self.assertTrue(menu._walk_menu(["Operations"], items, ctx, allow_back=False, allow_quit=True))
        self.assertEqual(seen[1][0], "Health")
        self.assertEqual(seen[1][1][0], ("Location", "Operations › Health"))


if __name__ == "__main__":
    unittest.main()
