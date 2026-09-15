from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ReleaseAutomationTests(unittest.TestCase):
    def test_release_manifest_and_python_version_are_consistent(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
        init = (ROOT / "src" / "tines_codex_cloud" / "__init__.py").read_text()
        runtime_version = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)

        self.assertIsNotNone(runtime_version)
        self.assertEqual(project["version"], manifest["."])
        self.assertEqual(project["version"], runtime_version.group(1))
        self.assertEqual(project["dependencies"], [])

    def test_release_please_uses_python_strategy_for_main(self) -> None:
        config = json.loads((ROOT / "release-please-config.json").read_text())
        workflow = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()

        self.assertEqual(config["release-type"], "python")
        self.assertFalse(config["include-component-in-tag"])
        self.assertEqual(config["packages"]["."]["package-name"], "tines-codex-cloud")
        self.assertIn("googleapis/release-please-action@v5", workflow)
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertRegex(workflow, r"branches:\s+- main")
        self.assertIn("release-please-config.json", workflow)
        self.assertIn(".release-please-manifest.json", workflow)

    def test_artifact_job_checks_released_tag_and_verifies_before_upload(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()

        self.assertIn("ref: ${{ needs.release.outputs.tag_name }}", workflow)
        self.assertIn("python -m build", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("python -m venv", workflow)
        self.assertIn("tines-codex-cloud --version", workflow)
        self.assertIn('tines-codex-cloud" --help', workflow)
        self.assertNotIn("pypi", workflow.lower())
        self.assertLess(workflow.index("python -m unittest"), workflow.index("python -m build"))
        self.assertLess(workflow.index("python -m build"), workflow.index("gh release upload"))

    def test_release_docs_use_github_assets_and_not_pypi(self) -> None:
        readme = (ROOT / "README.md").read_text()
        installation = (ROOT / "docs" / "installation.md").read_text()

        for document in (readme, installation):
            self.assertIn(
                "github.com/dstoc/tines-codex-cloud/releases/download/v0.1.0",
                document,
            )
            self.assertIn("pipx install", document)
        self.assertIn("not published\nto PyPI", installation)


if __name__ == "__main__":
    unittest.main()
