from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.history import update_history
from core.models import NodeResult


class HistoryTests(unittest.TestCase):
    def test_repeated_success_builds_history_and_failure_reduces_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            record = NodeResult(ip="104.16.1.1")
            options = {"window_runs": 4, "neutral_prior_runs": 2, "max_missed_runs": 8}

            for _ in range(3):
                update_history(path, [record], [record], options)
            self.assertEqual(record.history_runs, 3)
            self.assertEqual(record.history_success_rate, 1.0)
            self.assertEqual(record.history_consecutive_successes, 3)

            update_history(path, [record], [], options)
            state = json.loads(path.read_text(encoding="utf-8"))
            entry = state["nodes"][record.key]
            self.assertEqual(entry["samples"], [1, 1, 1, 0])
            self.assertEqual(entry["consecutive_successes"], 0)


if __name__ == "__main__":
    unittest.main()
