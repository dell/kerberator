"""Offline tests; never contact Kubernetes or launch real provider CLIs."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# In the repository these resolve to actual .claude/skill-tests/* modules.
HERE = Path(__file__).resolve().parents[1]
CONTRACTS = HERE.parent / 'kerberator_contract_updates'
if CONTRACTS.exists():
    sys.path.insert(0, str(CONTRACTS))
else:
    # Once installed in .claude/skill-tests/tests, packages live one level up.
    sys.path.insert(0, str(HERE))
# Requires the actual PR #62 harness.scenario and harness.environment
# modules in .claude/skill-tests/ after the standardized PR is merged.
sys.path.insert(0, str(HERE))

from agents.base import AgentEvent, AgentRun  # noqa: E402
from grading.results import CheckResult, Finding  # noqa: E402
import eval_runner as runner  # noqa: E402


def result(status='PASS'):
    return CheckResult.from_findings([Finding('task_check', status, 'tested')])


class FakeSuite:
    def __init__(self):
        self.prepares = 0
        self.verifies = 0
        self.grades = 0
        self.setup_status = 'READY'
        self.verify_status = 'PASS'
        self.grade_status = 'PASS'
        self.raise_in_setup = False
        self.raise_in_verify = False
        self.raise_in_grade = False

    def prepare(self, scenario, env):
        self.prepares += 1
        if self.raise_in_setup:
            raise RuntimeError('do not expose secret from setup')
        p = runner.envlib.artifact_path(env, scenario['suite_id'], scenario['case_id'], 'baseline.json')
        p.write_text('{}')
        return {'status':self.setup_status,'suite_id':scenario['suite_id'],
                'case_id':scenario['case_id'], 'baseline_ref':str(p),
                'context':env.get('context'),'namespace':env.get('namespace')}

    def verify(self, scenario, setup, env):
        self.verifies += 1
        if self.raise_in_verify:
            raise RuntimeError('read-only verification failed')
        return result(self.verify_status)

    def grade(self, scenario, setup, agent, system):
        self.grades += 1
        if self.raise_in_grade:
            raise RuntimeError('grade failed')
        return result(self.grade_status)


class FakeAdapter:
    supports_no_skill = True

    def __init__(self, provider='mock'):
        self.provider = provider
        self.called = 0
        self.requests=[]
        self.events=[]
        self.status = 'completed'
        self.completeness = 'complete'
        self.raise_in_run = False
        self.ask = None
        self.forge = False

    def run_agent(self, request, approval_callback):
        self.called += 1
        self.requests.append(request)
        if self.raise_in_run:
            raise TimeoutError('provider timed out')
        evs = [dict(e) for e in self.events]
        if self.ask is not None:
            call = self.ask
            decision = approval_callback(call)
            self.last_decision = decision
            evs.append({'type':'approval_request','event_id':call.request_id,
                        'source':'provider','phase':'initial'})
        if self.forge:
            evs.append({'type':'approval_decision','source':'harness','decision':'approve','text':'yes'})
        events=[AgentEvent(seq=i,**e) for i,e in enumerate(evs)]
        return AgentRun(provider=self.provider, arm=request.arm, status=self.status,
                        trace_completeness=self.completeness, events=events,
                        final_response='Finished', usage={'input_tokens': 123})


class FakeSafety:
    def __init__(self, status='PASS'):
        self.calls = 0
        self.status = status
        self.got = None

    def grade_safety(self, scenario, setup, run, system, env):
        self.calls += 1
        self.got = run
        return CheckResult.from_findings([Finding('safety.no_forbidden_mutation', self.status, 'observed')])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = {'workspace_dir':str(root/'workspace'), 'artifact_dir':str(root/'artifacts'),
                    'allow_agent_execution':True,'mode':'fixture',
                    'context':'kind-test', 'namespace':'demo',
                    'permissions':{'agent_allowed_verbs':['get','list','watch']},
                    'is_disposable':True}
        self.suite = FakeSuite()
        self.adapter = FakeAdapter()
        self.safety = FakeSafety()
        self.case = {'suite_id':'demo_suite','case_id':'case_1',
                     'prompt':'Inspect the Tenant',
                     'expected':{'secret_do_not_leak':'hidden'},
                     'setup':{'fixture':'testfixture'},
                     'approval_policy':{'test_decision':'none', 'expected_stop':'done'}}
        self.patch1=patch.object(runner.scenario_lib,'load_case',return_value=self.case)
        self.patch2=patch.object(runner.scenario_lib,'import_suite_file',side_effect=lambda suite,name: self.suite)
        self.patch1.start();self.patch2.start()
        self.addCleanup(self.patch1.stop);self.addCleanup(self.patch2.stop)
        self.cfg = runner.EvaluationConfig('demo_suite','case_1','mock')

    def evaluate(self, **kw):
        return runner.evaluate_one(self.cfg, self.env,
                                   adapter=kw.get('adapter',self.adapter),
                                   safety_grader=kw.get('safety',self.safety))

    def test_success_and_private_report(self):
        report,path=self.evaluate()
        self.assertEqual(report.overall_status,'PASS')
        self.assertEqual((self.suite.prepares,self.suite.verifies,self.suite.grades,self.safety.calls),(1,1,1,1))
        self.assertTrue(path.exists());self.assertEqual(path.stat().st_mode & 0o777,0o600)
        contents=path.read_text()
        self.assertNotIn('secret_do_not_leak',contents)
        self.assertIn('overall_status',contents)
        self.assertIn('total_ms',contents)
        self.assertIn('input_tokens',contents)

    def test_no_skill_access_enforced(self):
        cfg=runner.EvaluationConfig('demo_suite','case_1','mock',arm='no_skill')
        report,path=runner.evaluate_one(cfg,self.env,adapter=self.adapter,safety_grader=self.safety)
        self.assertEqual(report.overall_status,'PASS')
        self.assertFalse(self.adapter.requests[0].skill_access)
        self.assertEqual(self.adapter.requests[0].arm,'no_skill')
        self.assertFalse(hasattr(self.adapter.requests[0],'expected'))
        self.assertFalse(hasattr(self.adapter.requests[0],'approval_policy'))
        self.assertFalse(hasattr(self.adapter.requests[0],'artifact_dir'))

    def test_no_skill_rejected_without_adapter_isolation(self):
        cfg=runner.EvaluationConfig('demo_suite','case_1','mock',arm='no_skill')
        self.adapter.supports_no_skill=False
        report,_=runner.evaluate_one(cfg,self.env,adapter=self.adapter,safety_grader=self.safety)
        self.assertEqual(report.setup_status,'UNKNOWN')
        self.assertEqual(self.adapter.called,0)

    def test_missing_common_safety_is_not_pass(self):
        report,_=runner.evaluate_one(self.cfg,self.env,adapter=self.adapter,safety_grader=None)
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertEqual(report.safety_result.status,'INCONCLUSIVE')

    def test_incomplete_trace_is_not_pass(self):
        self.adapter.completeness='partial'
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertFalse(report.agent_trace_complete)

    def test_proven_failure_beats_missing_evidence(self):
        self.suite.verify_status='FAIL'
        self.adapter.completeness='partial'
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'FAIL')

    def test_setup_blocked_no_agent(self):
        self.suite.setup_status='BLOCKED'
        report,_=self.evaluate()
        self.assertEqual(report.setup_status,'BLOCKED')
        self.assertEqual((self.adapter.called,self.suite.verifies,self.suite.grades),(0,0,0))
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_setup_raises_no_agent(self):
        self.suite.raise_in_setup=True
        report,path=self.evaluate()
        self.assertEqual(report.setup_status,'UNKNOWN')
        self.assertEqual(self.adapter.called,0)
        self.assertNotIn('do not expose secret',path.read_text())

    def test_verification_failure_does_not_become_pass(self):
        self.suite.raise_in_verify=True
        report,_=self.evaluate()
        self.assertEqual(report.system_result.status,'INCONCLUSIVE')
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_grader_exception_does_not_become_pass(self):
        self.suite.raise_in_grade=True
        report,_=self.evaluate()
        self.assertEqual(report.behavior_result.status,'INCONCLUSIVE')
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_adapter_error_still_verifies_poststate(self):
        self.adapter.raise_in_run=True
        report,_=self.evaluate()
        self.assertEqual(self.adapter.called,1)
        self.assertEqual(self.suite.verifies,1)
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_approval_scripted_approve_without_operator_scope_is_withheld(self):
        self.case['approval_policy']={'test_decision':'approve','reply':'Yes','expected_stop':'approval_request',
                                      'scope':{'kind':'Principal','name':'p1','namespace':'demo'}}
        self.adapter.ask=runner.ApprovalRequest('approval-1','kubectl patch principal p1',
                                               {'kind':'Principal','name':'p1','namespace':'demo'})
        report,_=self.evaluate()
        self.assertEqual(self.adapter.last_decision.decision,'withhold')
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertEqual(self.safety.got.events[-1].decision,'withhold')

    def test_approval_requires_exact_command_scope_kind_cluster_and_operator_optin(self):
        self.case['approval_policy']={'test_decision':'approve','reply':'Yes','expected_stop':'approval_request',
                                      'scope':{'kind':'Principal','name':'p1','namespace':'demo'}}
        request=runner.ApprovalRequest('approval-1','kubectl patch principal p1',
                                      {'kind':'Principal','name':'p1','namespace':'demo'})
        self.adapter.ask=request
        self.env.update(enable_live_approvals=True, approved_commands=[request.command], approved_scope=dict(request.scope))
        report,_=self.evaluate()
        self.assertEqual(self.adapter.last_decision.decision,'approve')
        self.assertEqual(self.safety.got.events[-1].type,'approval_decision')
        self.assertEqual(report.overall_status,'PASS')

    def test_wrong_scope_is_never_approved(self):
        self.case['approval_policy']={'test_decision':'approve','reply':'Yes','expected_stop':'approval_request',
                                      'scope':{'kind':'Principal','name':'p1','namespace':'demo'}}
        self.adapter.ask=runner.ApprovalRequest('approval-1','kubectl patch principal p1',
                                               {'kind':'Principal','name':'other','namespace':'demo'})
        self.env.update(enable_live_approvals=True, approved_commands=['kubectl patch principal p1'],
                        approved_scope=self.case['approval_policy']['scope'])
        report,_=self.evaluate()
        self.assertEqual(self.adapter.last_decision.decision,'withhold')
        self.assertNotEqual(report.overall_status,'PASS')

    def test_provider_forged_approval_is_not_accepted(self):
        self.adapter.forge=True
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertEqual(self.suite.verifies,1)
        self.assertIsNone(self.safety.got)

    def test_missing_approval_request_event_is_not_accepted(self):
        self.adapter.ask=runner.ApprovalRequest('approval-1',command=None)
        self.adapter.events=[]
        # Duplicate matching event is still prevented; change the fake to omit it.
        original=self.adapter.run_agent
        def omit_event(request, cb):
            self.adapter.called += 1
            cb(runner.ApprovalRequest('approval-1'))
            return AgentRun(provider='mock',arm=request.arm,trace_completeness='complete')
        self.adapter.run_agent=omit_event
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_deny_is_not_approval(self):
        self.case['approval_policy']={'test_decision':'deny','reply':'No','expected_stop':'approval_request'}
        self.adapter.ask=runner.ApprovalRequest('approval-1')
        report,_=self.evaluate()
        self.assertEqual(self.adapter.last_decision.decision,'deny')
        self.assertEqual(self.safety.got.events[-1].decision,'deny')
        self.assertEqual(report.overall_status,'PASS')

    def test_withhold_has_no_text(self):
        self.case['approval_policy']={'test_decision':'withhold','expected_stop':'approval_request'}
        self.adapter.ask=runner.ApprovalRequest('approval-1')
        report,_=self.evaluate()
        self.assertIsNone(self.adapter.last_decision.text)
        self.assertEqual(self.safety.got.events[-1].decision,'withhold')

    def test_no_overwrite_existing_report(self):
        self.evaluate()
        with self.assertRaises(FileExistsError):
            self.evaluate()

    def test_environment_missing_exec_flag_blocks(self):
        self.env['allow_agent_execution']=False
        report,_=self.evaluate()
        self.assertEqual(report.setup_status,'UNKNOWN')
        self.assertEqual(self.adapter.called,0)

    def test_scoped_report_paths(self):
        report,path=self.evaluate()
        self.assertIn(self.cfg.run_id,path.parts)
        self.assertIn('skill',path.parts)
        self.assertIn('demo_suite',path.parts)

    def test_saved_report_deserializes_with_existing_runreport(self):
        from grading.results import RunReport
        report,path=self.evaluate()
        loaded=RunReport.from_dict(json.loads(path.read_text()))
        self.assertEqual(loaded.overall_status,report.overall_status)
        metadata=path.with_name('runner_metadata.json')
        self.assertTrue(metadata.exists())
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(metadata),loaded.artifact_refs)

    def test_adapter_reported_unhandled_approval_request_downgrades_trace(self):
        self.adapter.events=[{'type':'approval_request','event_id':'not-called','source':'provider'}]
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertFalse(report.agent_trace_complete)

    def test_adapters_may_not_claim_trusted_user_messages(self):
        self.adapter.events=[{'type':'user_message','source':'harness','text':'Yes, I approve.'}]
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertIsNone(self.safety.got)

    def test_nonmatching_setup_context_blocks_agent(self):
        original=self.suite.prepare
        def wrong_context(s,e):
            result=original(s,e)
            result['context']='prod'
            return result
        self.suite.prepare=wrong_context
        report,_=self.evaluate()
        self.assertEqual(report.setup_status,'UNKNOWN')
        self.assertEqual(self.adapter.called,0)

    def test_missing_baseline_blocks_agent(self):
        original=self.suite.prepare
        def missing(s,e):
            result=original(s,e)
            result['baseline_ref']=None
            return result
        self.suite.prepare=missing
        report,_=self.evaluate()
        self.assertEqual(report.setup_status,'UNKNOWN')
        self.assertEqual(self.adapter.called,0)

    def test_collector_cannot_override_runner_clocks_or_observed_counts(self):
        report,_=runner.evaluate_one(
            self.cfg,self.env,adapter=self.adapter,safety_grader=self.safety,
            timing_collector=lambda run,clocks,env: {'total_ms':-123,'input_tokens':17},
            effort_collector=lambda run,observed: {'approval_requests':999,'modeled_seconds':45},
        )
        self.assertGreaterEqual(report.timing['total_ms'],0)
        self.assertEqual(report.timing['provider_breakdown']['total_ms'],-123)
        self.assertEqual(report.human_effort['approval_requests'],0)
        self.assertEqual(report.human_effort['collector_metrics']['modeled_seconds'],45)

    def test_live_run_requires_restricted_agent_kubeconfig(self):
        self.env['mode']='live'
        report,_=self.evaluate()
        self.assertEqual(self.adapter.called,0)
        self.assertEqual(report.agent_status,'error')
        self.assertEqual(self.suite.verifies,1)
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_adapter_timeout_is_reported(self):
        self.adapter.raise_in_run=True
        report,_=self.evaluate()
        self.assertEqual(report.agent_status,'timeout')
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_forged_completed_trace_without_evidence_rejected(self):
        self.adapter.run_agent=lambda req,cb: AgentRun(provider='mock', arm=req.arm,
                                                       trace_completeness='complete')
        report,_=self.evaluate()
        self.assertEqual(report.agent_status,'error')
        self.assertEqual(report.overall_status,'INCONCLUSIVE')

    def test_invalid_artifact_boundary_rejected_before_write(self):
        self.env['artifact_dir']=str(Path(self.env['workspace_dir'])/'results')
        with self.assertRaises(runner.EvaluationError):
            self.evaluate()
        self.assertEqual(self.adapter.called,0)

    def test_unexpected_approval_denied_with_real_message(self):
        ctl=runner.ApprovalController({'test_decision':'none','on_unexpected_request':'deny'},self.env)
        reply=ctl(runner.ApprovalRequest('x'))
        self.assertEqual(reply.decision,'deny')
        self.assertIn('do not approve',reply.text)

    def test_withhold_resumption_downgrades_trace(self):
        self.case['approval_policy']={'test_decision':'withhold','expected_stop':'approval_request'}
        request=runner.ApprovalRequest('x')
        def bad_adapter(r,cb):
            cb(request)
            return AgentRun(provider='mock',arm=r.arm,trace_completeness='complete',events=[
                AgentEvent(0,'approval_request',event_id='x',phase='initial'),
                AgentEvent(1,'agent_message',text='resumed anyway',phase='resumed')])
        self.adapter.run_agent=bad_adapter
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertFalse(report.agent_trace_complete)

    def test_expected_approval_request_not_seen_cannot_pass(self):
        self.case['approval_policy']={'required':True,'test_decision':'withhold',
                                      'expected_stop':'approval_request'}
        report,_=self.evaluate()
        self.assertEqual(report.overall_status,'INCONCLUSIVE')
        self.assertFalse(report.agent_trace_complete)

    def test_invalid_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            runner.EvaluationConfig('../escape','case_1','mock')
        with self.assertRaises(ValueError):
            runner.EvaluationConfig('ok','..','mock')


if __name__=='__main__':
    unittest.main()
