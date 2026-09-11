import unittest
from unittest.mock import patch

from packetsafari_onprem import menu


class MenuPromptTests(unittest.TestCase):
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
