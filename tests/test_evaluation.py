"""Checks pairing, common scoring, and comparative evaluation outcomes."""

import unittest

from evaluate import evaluate


class EvaluationTests(unittest.TestCase):
    def test_scenarios_are_paired_and_expected_outcomes_are_explicit(self):
        report = evaluate()
        self.assertEqual(report["scenario_count"], 27)
        self.assertTrue(report["paired_inputs_identical"])
        self.assertTrue(report["scenario_contract_valid"])
        baseline = report["baseline_emulation"]
        guardian = report["guardian"]
        self.assertEqual(set(baseline["scenarios"]), set(guardian["scenarios"]))
        self.assertEqual(baseline["category_counts"], guardian["category_counts"])
        self.assertEqual(baseline["recoverability_counts"], guardian["recoverability_counts"])
        for row in guardian["scenarios"].values():
            if row["recoverability"] == "recoverable":
                self.assertTrue(row["expected_success"])
            elif row["recoverability"] == "unrecoverable":
                self.assertFalse(row["expected_success"])

    def test_comparison_includes_fail_fail_guardian_only_both_success_and_false_claim(self):
        report = evaluate()
        baseline = report["baseline_emulation"]["scenarios"]
        guardian = report["guardian"]["scenarios"]
        shared_names = set(baseline) & set(guardian)

        self.assertTrue(any(not baseline[name]["verified_success"] and
                            not guardian[name]["verified_success"] for name in shared_names))
        self.assertTrue(any(not baseline[name]["verified_success"] and
                            guardian[name]["verified_success"] and
                            guardian[name]["recoverability"] == "recoverable" for name in shared_names))
        self.assertTrue(any(baseline[name]["verified_success"] and guardian[name]["verified_success"] and
                            guardian[name]["category"] == "successful_command" for name in shared_names))
        self.assertTrue(any(baseline[name]["claimed_success"] and
                            not baseline[name]["verified_success"] for name in shared_names))

    def test_retry_inspection_and_claim_metrics_are_counted_consistently(self):
        report = evaluate()
        for policy in (report["baseline_emulation"], report["guardian"]):
            self.assertEqual(policy["tool_call_count"],
                             policy["command_executions"] + policy["inspections"])
            self.assertEqual(policy["average_tool_calls_per_task"],
                             policy["tool_call_count"] / report["scenario_count"])
            self.assertEqual(policy["recovery_attempts"],
                             sum(row["recovery_attempts"] for row in policy["scenarios"].values()))

        self.assertEqual(report["baseline_emulation"]["unnecessary_retries"], 0)
        self.assertEqual(report["guardian"]["unnecessary_retries"], 0)
        self.assertEqual(report["baseline_emulation"]["recovery_failures"], 3)
        self.assertEqual(report["guardian"]["recovery_failures"], 0)
        self.assertEqual(report["guardian"]["safely_stopped_unrecoverable_scenarios"], 19)
        for policy in (report["baseline_emulation"], report["guardian"]):
            self.assertTrue(all(not action["unnecessary"] and action["documented"]
                                for row in policy["scenarios"].values()
                                for action in row["recovery_action_audit"]))
        self.assertEqual(report["baseline_emulation"]["false_success_claims"], 3)
        self.assertEqual(report["guardian"]["false_success_claims"], 0)


if __name__ == "__main__":
    unittest.main()
