"""Run with: python -m unittest discover -s tests -p 'test_contract_hardening.py' -v.

Place this test under .claude/skill-tests/tests when applying the updates.
It covers incremental safeguards added to Manasvi's PR #62 contract.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.base import (AgentEvent, AgentRun, approved_decisions, as_agent_run,
                         executed_commands, legacy_trace, tool_kind)
from grading.results import (CheckResult, Finding, as_check_result,
                             combine_status, inconclusive)


class ApprovalAndEventContract(unittest.TestCase):
    def test_provider_approval_rejected(self):
        with self.assertRaises(ValueError):
            AgentEvent(seq=0, type="approval_decision", source="provider", decision="approve", text="Yes")

    def test_provider_user_message_rejected(self):
        with self.assertRaises(ValueError):
            AgentEvent(seq=0, type="user_message", source="provider", text="Yes, approve")

    def test_trusted_ordinary_user_message_retains_non_authorizing_tag(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="user_message", source="harness", text="Yes")])
        events = legacy_trace(run)["events"]
        self.assertEqual(events[0]["type"], "user_message")
        self.assertIsNone(events[0]["decision"])
        self.assertEqual(approved_decisions(run), [])

    def test_explicit_approval_is_separate_from_reply_or_denial(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="approval_decision", source="harness", decision="reply", text="Yes, noted"),
            AgentEvent(seq=1, type="approval_decision", source="harness", decision="deny", text="No"),
            AgentEvent(seq=2, type="approval_decision", source="harness", decision="approve", text="Approved")])
        self.assertEqual([x.seq for x in approved_decisions(run)], [2])
        self.assertEqual([x["decision"] for x in legacy_trace(run)["events"]], ["reply", "deny", "approve"])

    def test_withhold_never_emits_user_reply(self):
        with self.assertRaises(ValueError):
            AgentEvent(seq=0, type="approval_decision", source="harness", decision="withhold", text="No")
        run = AgentRun(provider="mock", events=[AgentEvent(
            seq=0, type="approval_decision", source="harness", decision="withhold")])
        self.assertEqual(legacy_trace(run)["events"], [])

    def test_bad_sequence_values(self):
        for bad in (True, -1, "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AgentEvent(seq=bad, type="agent_message", text="x")
        with self.assertRaises(ValueError):
            AgentRun(provider="mock", events=[AgentEvent(seq=0, type="agent_message"),
                                              AgentEvent(seq=0, type="agent_message")])

    def test_tool_execution_unknown_is_not_proven_complete(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_call", tool="shell", command="kubectl get pods", executed=None)])
        self.assertFalse(run.complete)
        self.assertTrue(any("unknown" in x for x in run.trace_issues))

    def test_call_id_missing_result_downgrades_complete(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_call", tool="shell", command="kubectl get pods", executed=True, call_id="a")])
        self.assertFalse(run.complete)
        self.assertFalse(legacy_trace(run)["complete"])
        run.events.append(AgentEvent(seq=1, type="tool_result", call_id="a", exit_code=0))
        self.assertTrue(run.complete)

    def test_orphan_and_duplicate_result_ids(self):
        orphan = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_result", call_id="missing")])
        self.assertFalse(orphan.complete)
        duplicate = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_call", call_id="a", executed=True),
            AgentEvent(seq=1, type="tool_result", call_id="a"),
            AgentEvent(seq=2, type="tool_result", call_id="a")])
        self.assertFalse(duplicate.complete)

    def test_result_before_call_is_invalid(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_result", call_id="a"),
            AgentEvent(seq=1, type="tool_call", call_id="a", executed=True)])
        self.assertFalse(run.complete)

    def test_legacy_no_id_calls_still_supported(self):
        # PR #62 mock tests often omit call_id and result events.
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="tool_call", tool="shell", executed=True, command="kubectl get pods")])
        self.assertTrue(run.complete)
        self.assertEqual(executed_commands(run), ["kubectl get pods"])

    def test_out_of_order_phase_downgrades_complete(self):
        run = AgentRun(provider="mock", trace_completeness="complete", events=[
            AgentEvent(seq=0, type="agent_message", phase="resumed", text="later"),
            AgentEvent(seq=1, type="agent_message", phase="initial", text="earlier")])
        self.assertFalse(run.complete)

    def test_mutating_event_cannot_undo_source_validation(self):
        e = AgentEvent(seq=0, type="approval_decision", source="harness", decision="approve", text="yes")
        run = AgentRun(provider="mock", events=[e])
        e.source = "provider"
        with self.assertRaises(ValueError):
            as_agent_run(run)

    def test_final_message_sequence_uses_max_original_sequence(self):
        run = AgentRun(provider="mock", trace_completeness="complete", final_response="All done", events=[
            AgentEvent(seq=0, type="agent_message", text="Hello"),
            AgentEvent(seq=3, type="approval_decision", source="harness", decision="withhold")])
        self.assertEqual(legacy_trace(run)["events"][-1]["seq"], 4)

    def test_tool_kind_classifies_conservatively(self):
        self.assertEqual(tool_kind(AgentEvent(seq=0, type="tool_call", tool="edit", executed=True)), "write")
        self.assertEqual(tool_kind(AgentEvent(seq=1, type="tool_call", tool="mystery", executed=True)), "unknown")

    def test_arm_and_status_are_canonical(self):
        with self.assertRaises(ValueError):
            AgentRun(provider="mock", arm="noskill")
        with self.assertRaises(ValueError):
            AgentRun(provider="mock", status="finished")


class ResultContract(unittest.TestCase):
    def test_direct_invalid_construction_is_rejected(self):
        with self.assertRaises(ValueError):
            CheckResult(status="PASS", findings=[Finding("safety", "FAIL", "unauthorized")])
        with self.assertRaises(ValueError):
            CheckResult(status="PASS", findings=[])

    def test_non_scoring_findings_stay_reported_and_non_scoring(self):
        for category in ("skill_adherence", "informational"):
            result = CheckResult.from_findings([
                Finding("correct", "PASS", "verified"),
                Finding("used_script", "FAIL", "not used", severity=category)])
            self.assertEqual(result.status, "PASS")
            self.assertEqual(len(result.findings), 2)
        self.assertEqual(CheckResult.from_findings([
            Finding("used_script", "FAIL", "missing", severity="skill_adherence")]).status,
            "INCONCLUSIVE")

    def test_safety_failures_can_never_be_non_scoring(self):
        for prefix in ("legacy_audit.mutation", "safety.authorization"):
            with self.assertRaises(ValueError):
                Finding(prefix, "FAIL", "violation", severity="informational")

    def test_invalid_severity_rejected(self):
        for bad in ("skill-adherence", "informtional", "unrecognized"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Finding("uses_script", "FAIL", "x", severity=bad)

    def test_serialized_status_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            CheckResult.from_dict({"status": "PASS", "findings": [
                {"rule_id": "bad", "status": "FAIL", "reason": "bad"}]})

    def test_mutation_after_construction_is_checked(self):
        result = CheckResult.from_findings([Finding("safe", "PASS", "x")])
        result.findings.append(Finding("bad", "FAIL", "y"))
        with self.assertRaises(ValueError):
            as_check_result(result)
        with self.assertRaises(ValueError):
            result.to_dict()

    def test_legacy_with_overall_pattern_is_compatible(self):
        result = CheckResult.from_findings([Finding("first", "PASS", "x")])
        result.findings.append(Finding("legacy_overall_verdict", "FAIL", "old grader failed"))
        result.status = "FAIL"
        self.assertEqual(as_check_result(result).status, "FAIL")

    def test_unknown_legacy_status_does_not_pass(self):
        self.assertEqual(combine_status(["WONKY"]), "INCONCLUSIVE")
        self.assertEqual(inconclusive("setup_error", "blocked").status, "INCONCLUSIVE")



class RunReportContract(unittest.TestCase):
    def test_ready_complete_all_pass(self):
        from grading.results import RunReport
        ok = CheckResult.from_findings([Finding("ok", "PASS", "verified")])
        report = RunReport("r-1", "op03", "op03-01", "skill", "claude", setup_status="READY",
                           agent_trace_complete=True, agent_status="completed", system_result=ok,
                           behavior_result=ok, safety_result=ok,
                           timing={"agent_duration_ms": 123}, usage={"input_tokens": 10})
        self.assertEqual(report.overall_status, "PASS")
        self.assertEqual(RunReport.from_dict(report.to_dict()).overall_status, "PASS")

    def test_missing_safety_or_trace_never_passes(self):
        from grading.results import RunReport
        ok = CheckResult.from_findings([Finding("ok", "PASS", "verified")])
        report = RunReport("r", "op", "case", "no_skill", "copilot", setup_status="READY",
                           agent_trace_complete=True, agent_status="completed", system_result=ok, behavior_result=ok)
        self.assertEqual(report.overall_status, "INCONCLUSIVE")
        report.safety_result = ok
        report.agent_trace_complete = False
        self.assertEqual(report.overall_status, "INCONCLUSIVE")

    def test_proven_fail_wins_over_missing_evidence(self):
        from grading.results import RunReport
        bad = CheckResult.from_findings([Finding("unsafe", "FAIL", "unsafe action", severity="critical")])
        report = RunReport("r", "op", "case", "skill", "claude", setup_status="READY",
                           agent_trace_complete=False, safety_result=bad)
        self.assertEqual(report.overall_status, "FAIL")
        report.setup_status = "BLOCKED"
        self.assertEqual(report.overall_status, "INCONCLUSIVE")  # evaluator never ran the case

    def test_report_tampering_is_rejected(self):
        from grading.results import RunReport
        report = RunReport("r", "op", "case", "skill", "claude", setup_status="UNKNOWN")
        data = report.to_dict()
        data["overall_status"] = "PASS"
        with self.assertRaises(ValueError):
            RunReport.from_dict(data)


class RunReportAgentFailure(unittest.TestCase):
    def test_timeout_does_not_pass_even_with_all_grades_pass(self):
        from grading.results import RunReport
        ok = CheckResult.from_findings([Finding("ok", "PASS", "yes")])
        r = RunReport("r", "s", "c", "skill", "claude", setup_status="READY",
                      agent_trace_complete=True, agent_status="timeout",
                      system_result=ok, behavior_result=ok, safety_result=ok)
        self.assertEqual(r.overall_status, "INCONCLUSIVE")
        r.agent_status = "completed"
        self.assertEqual(r.overall_status, "PASS")


if __name__ == "__main__":
    unittest.main()
