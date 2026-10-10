import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_gate import build_decision_gate  # noqa: E402
from data_quality import (  # noqa: E402
    BLOCKED,
    CONSERVATIVE_DELAYED,
    CONSERVATIVE_FALLBACK,
    LIMITED,
    READY,
)


class DecisionGateTests(unittest.TestCase):
    def test_ready_allows_standard_actions(self) -> None:
        result = build_decision_gate({"gate": READY})
        self.assertEqual(result["gate"], READY)
        self.assertTrue(result["can_analyze"])
        self.assertTrue(result["can_buy"])
        self.assertTrue(result["can_strong_buy"])
        self.assertTrue(result["can_exact_reduce"])

    def test_qdii_delay_allows_conservative_analysis_only(self) -> None:
        result = build_decision_gate(
            {"gate": LIMITED, "analysis_mode": CONSERVATIVE_DELAYED}
        )
        self.assertTrue(result["can_analyze"])
        self.assertFalse(result["can_buy"])
        self.assertFalse(result["can_strong_buy"])
        self.assertFalse(result["can_exact_reduce"])
        self.assertEqual(result["mode"], CONSERVATIVE_DELAYED)

    def test_fallback_is_observation_only(self) -> None:
        result = build_decision_gate(
            {"gate": LIMITED, "analysis_mode": CONSERVATIVE_FALLBACK}
        )
        self.assertTrue(result["can_analyze"])
        self.assertFalse(result["can_buy"])
        self.assertEqual(result["mode"], CONSERVATIVE_FALLBACK)

    def test_blocked_overrides_a_ready_item(self) -> None:
        result = build_decision_gate(
            {"gate": READY}, {"overall_gate": BLOCKED, "summary": "overall blocked"}
        )
        self.assertEqual(result["gate"], BLOCKED)
        self.assertFalse(result["can_analyze"])
        self.assertFalse(result["can_buy"])
        self.assertFalse(result["can_strong_buy"])
        self.assertFalse(result["can_exact_reduce"])


if __name__ == "__main__":
    unittest.main()
