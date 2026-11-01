"""Guards for the /setup command spec.

The command is a markdown spec (the spec IS the implementation). These tests pin
one invariant that broke silently: Step 3 must personalise every contact block
that `/apply` later compiles into a document. `cv/main_example.tex` was covered;
the LaTeX blocks embedded in `05-cv-templates.md` and `06-cover-letter-templates.md`
were not, so a full Path B/C run left `[YOUR_NAME]`, `[YOUR_EMAIL]` and
`[YOUR_PHONE]` in both, and whether they reached a compiled cover letter depended
on the drafter noticing. A real user (#420) ran `/setup` and then hand-edited both
files to close the gap.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

UPSTREAM = "MadsLorentzen/ai-job-search"

REPO = Path(__file__).resolve().parent.parent
COMMAND = REPO / ".claude" / "commands" / "setup.md"
SKILL_DIR = REPO / ".claude" / "skills" / "job-application-assistant"
CV_TEMPLATES = SKILL_DIR / "05-cv-templates.md"
COVER_TEMPLATES = SKILL_DIR / "06-cover-letter-templates.md"


def _sections(text: str) -> dict[str, str]:
    """Split a command spec into {heading: body} by '## ' headers."""
    parts = text.split("\n## ")
    result = {}
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        result[heading.strip()] = body
    return result


def _substeps(step_body: str) -> dict[str, str]:
    """Split a step body into {'### N. ...' heading: body}."""
    parts = step_body.split("\n### ")
    result = {}
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        result[heading.strip()] = body
    return result


class SetupStep3ContactBlocks(unittest.TestCase):
    def setUp(self):
        self.step3 = _sections(COMMAND.read_text(encoding="utf-8"))["Step 3: Generate Profile Files"]
        self.substeps = _substeps(self.step3)

    def _substep_for(self, filename: str) -> str:
        matches = [body for heading, body in self.substeps.items() if filename in heading]
        self.assertEqual(len(matches), 1, f"expected exactly one Step 3 substep for {filename}, got {len(matches)}")
        return matches[0]

    def test_cv_templates_substep_fills_the_contact_block(self):
        body = self._substep_for("05-cv-templates.md")
        self.assertIn("contact", body.lower())
        for token in ("[FIRST_NAME]", "[YOUR_EMAIL]", "[YOUR_PHONE]"):
            self.assertIn(token, body, f"the 05 substep must name {token} as something to replace")

    def test_cover_letter_templates_get_their_own_substep(self):
        body = self._substep_for("06-cover-letter-templates.md")
        self.assertIn("signature", body.lower())
        for token in ("[YOUR_NAME]", "[YOUR_EMAIL]", "[YOUR_PHONE]", "[YOUR_LINKEDIN_URL]"):
            self.assertIn(token, body, f"the 06 substep must name {token} as something to replace")

    def test_completion_summary_lists_the_cover_letter_templates(self):
        step4 = _sections(COMMAND.read_text(encoding="utf-8"))["Step 4: Confirm & Next Steps"]
        summary = step4.split("**Privacy note:**")[0]
        self.assertIn("06-cover-letter-templates.md", summary)


@unittest.skipIf(
    os.environ.get("GITHUB_REPOSITORY", UPSTREAM) != UPSTREAM,
    "placeholder-integrity guards the pristine upstream template; forks personalize these files via /setup",
)
class TemplatesStillCarryThePlaceholders(unittest.TestCase):
    """The instructions above target real tokens; if a template renames them,
    the instruction and this test must move together."""

    def test_cv_templates_contact_block_tokens(self):
        text = CV_TEMPLATES.read_text(encoding="utf-8")
        for token in ("[FIRST_NAME]", "[LAST_NAME]", "[YOUR_EMAIL]", "[YOUR_PHONE]"):
            self.assertIn(token, text)

    def test_cover_letter_templates_contact_and_signature_tokens(self):
        text = COVER_TEMPLATES.read_text(encoding="utf-8")
        for token in ("[YOUR_NAME]", "[YOUR_EMAIL]", "[YOUR_PHONE]", "[YOUR_LINKEDIN_URL]"):
            self.assertIn(token, text)
        self.assertIn("\\signature{[YOUR_NAME]}", text)


class ForkSkipRegression(unittest.TestCase):
    """TemplatesStillCarryThePlaceholders must skip (not fail) on forks.

    Regression test for the red fork-CI run: the placeholder assertions
    only hold on the pristine upstream template, so under a fork
    GITHUB_REPOSITORY the class must report skipped and exit 0.
    """

    def test_placeholder_class_skips_on_forks(self):
        env = dict(os.environ, GITHUB_REPOSITORY="someone/fork")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.test_setup_command.TemplatesStillCarryThePlaceholders", "-v"],
            capture_output=True, text=True, cwd=REPO, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("skipped", proc.stderr)


if __name__ == "__main__":
    unittest.main()
