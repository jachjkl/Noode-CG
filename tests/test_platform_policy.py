from __future__ import annotations

import unittest

from core.isp_test import apply_platform_policy
from core.models import NodeResult


class PlatformPolicyTests(unittest.TestCase):
    def test_strict_policy_keeps_only_nodes_passing_every_platform(self) -> None:
        good = NodeResult(ip="104.16.1.1")
        good.probe_results["cm-local"] = {"platforms": {"x": True, "github": True}}
        bad = NodeResult(ip="104.16.1.2")
        bad.probe_results["cm-local"] = {"platforms": {"x": False, "github": True}}

        selected = apply_platform_policy(
            [good, bad],
            {"required": True, "required_platforms": ["x", "github"], "minimum_vantages": 1},
        )

        self.assertEqual([node.key for node in selected], [good.key])
        self.assertTrue(good.platform_ok)
        self.assertFalse(bad.platform_ok)

    def test_unknown_platforms_do_not_fail_when_policy_is_advisory(self) -> None:
        node = NodeResult(ip="104.16.1.1")
        selected = apply_platform_policy(
            [node],
            {"required": False, "required_platforms": ["x", "github"], "minimum_vantages": 1},
        )
        self.assertEqual(selected, [node])
        self.assertIsNone(node.platform_ok)


if __name__ == "__main__":
    unittest.main()
