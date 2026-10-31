"""Tests for tools/sweep-runner.py - the scheduled-sweep entrypoint.

The sweep subprocess is always mocked here: these tests cover the runner's
own cadence/artifact plumbing, never live discovery.
"""

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "sweep_runner", REPO / "tools" / "sweep-runner.py")
sweep_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep_runner)

DATE = "2026-10-31"


def _run_main(artifacts, root=None, returncode=0):
    argv = ["--force", "--date", DATE, "--artifacts", str(artifacts)]
    out = io.StringIO()
    result = mock.Mock(returncode=returncode)
    with mock.patch.object(sweep_runner.subprocess, "run",
                           return_value=result), \
            redirect_stdout(out), redirect_stderr(io.StringIO()):
        if root is None:
            rc = sweep_runner.main(argv)
        else:
            with mock.patch.object(sweep_runner, "ROOT", root):
                rc = sweep_runner.main(argv)
    return rc, out.getvalue()


class ArtifactDisplay(unittest.TestCase):
    def test_outside_root_artifacts_prints_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / f"sweep-{DATE}.md").write_text("# proof\n", encoding="utf-8")
            rc, out = _run_main(artifacts)
            self.assertEqual(rc, 0)
            self.assertIn(f"artifact={artifacts / f'sweep-{DATE}.md'}", out)

    def test_inside_root_artifacts_prints_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = root / "reports" / "sweeps"
            artifacts.mkdir(parents=True)
            (artifacts / f"sweep-{DATE}.md").write_text("# proof\n", encoding="utf-8")
            rc, out = _run_main(artifacts, root=root)
            self.assertEqual(rc, 0)
            self.assertIn(f"artifact=reports/sweeps/sweep-{DATE}.md", out)

    def test_failed_sweep_propagates_exit_code(self):
        rc, _ = _run_main("/nonexistent", returncode=3)
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
