from __future__ import annotations

import unittest
from pathlib import Path


class MigrationWorkflowTests(unittest.TestCase):
    def test_cleanup_removes_obsolete_pipeline_loop_test(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "cleanup-legacy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("tests/test_pipeline_loop.py", workflow)

        ci = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("rm -f -- tests/test_pipeline_loop.py", ci)


if __name__ == "__main__":
    unittest.main()
