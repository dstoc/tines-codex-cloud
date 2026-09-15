from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ReleaseAutomationTests(unittest.TestCase):
    def test_release_documentation_describes_conventional_commit_trigger(self) -> None:
        config = json.loads((ROOT / "release-please-config.json").read_text())
        manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        documentation = (ROOT / "docs" / "installation.md").read_text()

        self.assertEqual(config["release-type"], "python")
        self.assertEqual(config["packages"]["."]["package-name"], project["name"])
        self.assertEqual(manifest["."], project["version"])
        self.assertIn("Release Please watches commits merged into `main`.", documentation)
        self.assertIn("feat(release): publish the bridge release", documentation)
        self.assertIn("do not edit those generated release files by hand", documentation)


if __name__ == "__main__":
    unittest.main()
