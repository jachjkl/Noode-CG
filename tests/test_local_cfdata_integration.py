from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LocalCfDataIntegrationTests(unittest.TestCase):
    def test_runner_uses_official_mode_without_embedded_github_token(self) -> None:
        runner = (ROOT / "scripts" / "run-local-cfdata.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('"-mode=official"', runner)
        self.assertIn('"-scanmode=tcping"', runner)
        self.assertIn('"-offspeedlimit=$CandidateTarget"', runner)
        self.assertIn('"-offspeedmin=$MinimumSpeedMBps"', runner)
        self.assertIn('"-offout=$CsvName"', runner)
        self.assertIn('"-github=false"', runner)
        self.assertNotIn("-ghtoken=", runner.lower())
        self.assertIn("git -C $RepoRoot add -- data/local-cfdata-candidates.txt", runner)

    def test_startup_task_waits_for_network(self) -> None:
        installer = (ROOT / "scripts" / "install-local-cfdata-task.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("-AtLogOn", installer)
        self.assertIn("-RunOnlyIfNetworkAvailable", installer)
        self.assertIn("-StartWhenAvailable", installer)

    def test_clean_package_excludes_dynamic_local_candidates(self) -> None:
        packager = (ROOT / "scripts" / "package.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("local-cfdata-candidates", packager)
        self.assertIn("previous-top100", packager)
        self.assertIn("previous-official-ips", packager)


if __name__ == "__main__":
    unittest.main()
