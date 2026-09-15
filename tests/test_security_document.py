from pathlib import Path
import re
import unittest


SECURITY_DOCUMENT = Path(__file__).parents[1] / "SECURITY.md"


class SecurityDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = SECURITY_DOCUMENT.read_text(encoding="utf-8")

    def test_records_the_required_security_decision_sections(self) -> None:
        for heading in (
            "## Decision",
            "## Threat model",
            "## Evaluation of the four options",
            "## Production design",
            "### Revocation and cleanup",
            "### Logging and retention contract",
            "## Acceptance tests and rollout gate",
        ):
            self.assertIn(heading, self.document)

    def test_recommends_an_out_of_prompt_handoff(self) -> None:
        self.assertIn("first-class Tines remote-runner handoff", self.document)
        self.assertIn("attested, scoped relay", self.document)
        self.assertIn("Not suitable for runtime Tines API access", self.document)

    def test_document_contains_no_literal_run_key_assignment(self) -> None:
        self.assertNotRegex(
            self.document,
            re.compile(r"TINES_API_KEY\s*=\s*(?:tines_)?[A-Za-z0-9][A-Za-z0-9_-]{15,}"),
        )
        self.assertNotIn("export TINES_API_KEY=", self.document)


if __name__ == "__main__":
    unittest.main()
