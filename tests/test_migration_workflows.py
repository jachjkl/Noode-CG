from __future__ import annotations

import unittest
from pathlib import Path


class MigrationWorkflowTests(unittest.TestCase):
    def test_cleanup_removes_obsolete_pipeline_loop_test(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "cleanup-legacy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("tests/test_pipeline_loop.py", workflow)
        self.assertIn("scripts/run-local-cfdata.ps1", workflow)
        self.assertIn("data/local-cfdata-candidates.txt", workflow)
        self.assertNotIn("            output/nodes.txt\n", workflow)
        self.assertNotIn("            output/nodes.json\n", workflow)
        self.assertNotIn("            data/previous-top100.json\n", workflow)

        ci = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("rm -f -- tests/test_pipeline_loop.py", ci)

        package_script = (
            Path(__file__).parents[1] / "scripts" / "package.ps1"
        ).read_text(encoding="utf-8")
        for name in ("nodes\\.txt", "nodes\\.json", "nodes\\.csv", "api\\.json", "health\\.json"):
            self.assertIn(name, package_script)
        self.assertIn(r"data[\\/]handoff", package_script)


if __name__ == "__main__":
    unittest.main()
