import tempfile
import unittest
from pathlib import Path

import ticket_bot


class CachedChromeDriverTests(unittest.TestCase):
    def test_prefers_driver_with_matching_chrome_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir)
            older_driver = (
                cache_root
                / "chromedriver"
                / "win64"
                / "151.0.7921.99"
                / "chromedriver.exe"
            )
            matching_driver = (
                cache_root
                / "chromedriver"
                / "win64"
                / "151.0.7922.138"
                / "chromedriver.exe"
            )
            for driver in (older_driver, matching_driver):
                driver.parent.mkdir(parents=True)
                driver.touch()

            result = ticket_bot.find_cached_chromedriver(
                "151.0.7922.170", cache_root
            )

        self.assertEqual(result, str(matching_driver))


if __name__ == "__main__":
    unittest.main()
