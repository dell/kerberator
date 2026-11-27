# Kerberator shared evaluation runner — new implementation

This is a replacement for the old **OP-04-specific** `.claude/skill-tests/eval_runner.py`.
It is written against the **PR #62 standardized suite contract** and the **updated**
`agents/base.py` and `grading/results.py` that contain `RunReport` and contract hardening.

The design follows the team evaluation plan: **one runner for one case**, separate
suite setup / independent verification / behavior grading / safety grading, and
a *different* future `benchmark_runner.py` for repeated or skill-vs-no-skill experiments.

## Copy these files after PR #62 is merged

```text
kerberator/
└── .claude/
    └── skill-tests/
        ├── eval_runner.py                 <-- REPLACE old OP-04-specific runner
        ├── harness/
        │   ├── __init__.py                <-- existing from PR #62, keep it
        │   ├── environment.py             <-- existing from PR #62, keep it
        │   ├── scenario.py                <-- existing from PR #62, keep it
        │   └── approval.py                <-- NEW: trusted approval delivery
        ├── agents/
        │   ├── base.py                    <-- your hardened file from earlier package
        │   ├── copilot.py                 <-- Manasvi to implement
        │   └── claude.py                  <-- Manasvi to implement
        ├── grading/
        │   ├── results.py                 <-- your hardened file containing RunReport
        │   └── common.py                  <-- Anvesha to implement
        ├── timing/                        <-- Saksham's modules, separate
        ├── suites/                        <-- all existing standardized suites
        └── tests/
            ├── test_suite_contracts.py    <-- Manasvi's tests from PR #62
            ├── test_contract_hardening.py <-- earlier update package
            └── test_eval_runner.py        <-- NEW offline runner tests
```

**Important**: PR #62 does not standardize OP-04's original
`kerberator-rotate-keytab/` directory. The old OP-04 CLI commands will stop
working once `eval_runner.py` is replaced. Migrate OP-04 to the four-file
suite contract first if you still need automated OP-04 evaluation. OP-10 is
stacked in a separate PR.

## Public API and ownership

### Runner — coordinator (Saniya)

```python
from eval_runner import EvaluationConfig, evaluate_one

config = EvaluationConfig(
    suite_id="dg01_kerberator_diagnose_tenant",
    case_id="dg01-01-healthy",
    provider="copilot",
    arm="skill",              # or "no_skill"
    model=None,
)
report, report_path = evaluate_one(
    config, trusted_environment,
    adapter=copilot_module,
    safety_grader=common_module,
    timing_collector=spans.collect_timing,       # optional, when implemented
    effort_collector=human.summarize_human_effort, # optional, when implemented
)
print(report.overall_status, report_path)
```

Calls, in order:

1. Validate trusted config, suite and case, and import that suite's modules.
2. Call `setup.prepare(scenario, environment)`, ensure a READY baseline exists.
3. Build `ExecutionRequest` from only the public prompt, scoped execution
   settings, and **restricted agent** kubeconfig. Never pass private expected
   values, evaluator kubeconfig, fixture policy, or artifact directory.
4. Call exactly **one agent adapter**, which may internally pause/resume after
   calling the runner-owned approval callback. The full run becomes one
   `AgentRun` with trusted approval events inserted by the runner.
5. Call independent `verify.verify(...)` (even after adapter failure), then
   suite `grade.grade(...)`, then shared safety `grade_safety(...)`.
6. Build a `RunReport` and write two private files:
   `run_report.json` and `runner_metadata.json`.

Setup BLOCKED/UNKNOWN -> no agent invocation. An exception in a verifier or
grader -> INCONCLUSIVE for that layer. A confirmed FAIL in another layer is
never erased by missing evidence. **Without the shared safety grader, a run
cannot report PASS.** All saved `run_report.json` files are directly readable
by `RunReport.from_dict()`.

### Provider adapters — Manasvi

Export a module-level function:

```python
supports_no_skill = True  # only when the adapter ACTUALLY enforces isolation

def run_agent(request: ExecutionRequest,
              approval_callback: Callable[[ApprovalRequest], ApprovalDecision]
              ) -> AgentRun:
    ...
```

- `request` contains: `prompt`, `provider`, `arm`, `model`,
  `timeout_seconds`, `workspace_dir`, `context`, `namespace`,
  `agent_kubeconfig_path`, `allowed_verbs`, `skill_access`, and `run_id`.
- A **no-skill** adapter must actually hide `SKILL.md` and scripts and disallow
  the Skill tool; the `skill_access` flag is a requirement, not proof.
- When the agent asks for approval, emit an `AgentEvent(type="approval_request",
  event_id=<unique request id>, ...)` and call
  `approval_callback(ApprovalRequest(request_id=<same id>, command=<exact string>,
  scope=<structured exact target>))`. The returned `ApprovalDecision` controls
  whether or how the provider session is resumed.
- **Do not** fabricate `approval_decision` or `user_message` AgentEvents, even
  with source `"harness"`. The runner inserts trusted approval decisions.
- `withhold` means **no reply** and **no resume**. `deny` delivers an explicit
  negative reply. For `approve`, the exact command/scope must match both the
  scenario policy and the operator's explicit trusted allowlist.
- Preserve `call_id`, executed vs proposed tool status, completion results,
  real timestamps, session continuity, actual CLI model/version, usage and
  private raw trace references. `trace_completeness="complete"` must be
  justified by the provider evidence. A tool-call without its result is not
  a trustworthy complete trace.
- Return the requested `provider` and `arm`, and never expose the evaluator's
  kubeconfig to the agent.
- If the adapter has no trustworthy way to pause before mutation, it must
  **not** enable automatic live approvals.

### Common safety grader — Anvesha

Export:

```python
def grade_safety(scenario: dict, setup_result: dict,
                 agent_run: AgentRun | None,
                 system_result: CheckResult | None,
                 environment: dict) -> CheckResult:
    ...
```

A positive safety result requires verified evidence of authorization *before*
the exact mutating operation and within resource/action scope. A later correct
Kubernetes state does not erase an unauthorized mutation. With missing tool
trace/approval evidence, return INCONCLUSIVE, not PASS. This file is **not**
implemented in this package. Existing suite-local `legacy_audit.*` findings
remain in place until shared grading is independently tested.

### Timing / human effort — Saksham

Optional function contracts:

```python
def collect_timing(agent_run: AgentRun, runner_clocks_ms: dict,
                   environment: dict) -> dict: ...

def summarize_human_effort(agent_run: AgentRun,
                           observed_counts: dict) -> dict: ...
```

Runner stage times use `time.perf_counter()`: `setup_ms`, `agent_ms`,
`verification_ms`, `grading_ms`, `safety_ms`, and `total_ms`.
Provider timing breakdown goes in `timing.provider_breakdown`, never overwrites
runner clocks. Human effort goes in `human_effort.collector_metrics`; the
runner records only **observed approval counts**, not guessed human minutes.
Usage passes through the normalized `AgentRun.usage` field; unsupported tokens
and cost are absent, **not zero**.

## CLI

From the repo root, after merging contracts and adding an adapter:

```bash
# No cluster access or agent execution:
python .claude/skill-tests/eval_runner.py list --suite dg01_kerberator_diagnose_tenant
python .claude/skill-tests/eval_runner.py validate --suite dg01_kerberator_diagnose_tenant

# One run (requires an authorized, trusted environment file):
python .claude/skill-tests/eval_runner.py run \
  --suite dg01_kerberator_diagnose_tenant \
  --case dg01-01-healthy \
  --provider copilot --arm skill \
  --environment /secure/eval-environment.json \
  --adapter-module agents.copilot \
  --safety-module grading.common \
  --execute
```

The CLI exits with code 0 PASS, 1 FAIL, 2 INCONCLUSIVE, or 3 for runner/CLI
failure. `--execute` **and** `allow_agent_execution: true` in the operator
config are required. If an adapter or common grader is not implemented,
please do **not** substitute a fabricated PASS-returning grader.

## Trusted environment

`examples/eval-environment.example.json` is a deliberately non-executable
example. Copy it to a **private path outside the agent workspace** and fill in
real restricted kubeconfig(s) and the correct authorized context. Do not store
credentials or a live `allow_agent_execution: true` config in Git.

Safety layers:

- Normal suite fixture mutation requires PR #62's
  `allow_fixture_mutation`, `is_disposable`, and `allowed_fixture_ids`, plus
  an actually restricted evaluator credential and verified disposable context.
- The agent must receive a **different restricted identity** from the evaluator
  (`agent_kubeconfig_path` or approved `agent_inputs.kubeconfig`). RBAC must
  enforce allowed operations; Python checks do not make a Kubernetes sandbox.
- Scripted `test_decision: approve` **never itself grants live authorization**.
  The operator must set `enable_live_approvals: true`, `is_disposable: true`,
  a `kind-*` context, `approved_scope` exactly matching the scenario and
  request scopes, and `approved_commands` containing the exact requested
  command. Otherwise the approval is withheld and the run cannot PASS.
- Experimental `run_id` and `arm` are inserted into `environment` so PR #62
  isolates baseline and review artifacts. The suite is responsible for resetting
  and verifying the starting state; the runner does not reset shared clusters.
- All evaluator output is private (0600 files and a 0700 report directory);
  never save keytab bytes, Secret contents, or raw CLI outputs in JSON reports.

This package does NOT implement a security sandbox, live cluster identity
attestation, provider CLI adapter, cluster-RBAC provisioning, shared safety
grade, approval-scope inference, or cross-run benchmark comparison. Those are
separate, required integration tasks.

## Test / integration sequence

```bash
# Offline evaluator tests (should not contact Kubernetes):
python -m unittest discover -s .claude/skill-tests/tests -p 'test_eval_runner.py' -v
python -m unittest discover -s .claude/skill-tests/tests -p 'test_contract_hardening.py' -v
python -m unittest discover -s .claude/skill-tests/tests -p 'test_suite_contracts.py' -v

# Then one explicitly authorized fixture-mode smoke run, with a mock adapter.
# Only after Anvesha's grader and Manasvi's real adapters are tested: try
# live read-only DG cases, and finally disposable, approval-gated OP cases.
```

The runner's 35 new tests have been run locally with a minimal local shim for
PR #62's `harness.scenario` and `harness.environment` because the complete
repository is unavailable in this container. **They have not yet been run
against the merged repository or a live Kubernetes cluster.** Run the entire
contract test suite after PR #62 merges, and review all failures before opening
a PR for this runner.

## What is intentionally reused from the old OP-04 runner

Only the good ideas: **private artifacts**, refusal to overwrite existing
reports, conservative INCONCLUSIVE for unknown evidence, and explicit approval
and execution controls. Its OP-04-specific scenario parser, shell/JSONL trace
parsers, command regexes, snapshots and grading are intentionally not reused.
