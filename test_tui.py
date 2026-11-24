import unittest

from jetbrains_proxy import discover_ides
from jetbrains_proxy_tui import ConfirmScreen, JetBrainsProxyApp


class TextualSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_selection_and_confirmation(self):
        installations = discover_ides([])
        if not installations:
            self.skipTest("No IDE installations available")
        app = JetBrainsProxyApp(installations, [], False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#installations").row_count, len(installations))
            await pilot.press("space")
            self.assertEqual(len(app.selected), 1)
            await pilot.press("a")
            self.assertEqual(len(app.selected), len(installations))
            await pilot.press("a")
            self.assertEqual(len(app.selected), 0)
            await pilot.press("space")
            await pilot.press("p")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            app.screen.dismiss(False)
            await pilot.pause()
            self.assertFalse(app.busy)


if __name__ == "__main__":
    unittest.main()
