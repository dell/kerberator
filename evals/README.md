# Kerberator agent evals

Evals check whether an agent can carry out a Kerberator task, not just whether a command works. Each eval gives the agent a realistic prompt on a cluster in a known state. It then grades two things:

- **behaviour:** a judge reads the transcript against the `expected` list. There is no automated judge yet; grade by hand for now;
- **the real result:** a `verify` script inspects the cluster.

A case passes only if both pass. The process is described in *Kerberator_Agent_Testing_Process*.

Skills live in `.claude/skills/`, where Claude Code finds them. The evals are kept here, outside the skill folders, so an agent reading a skill cannot see the expected answers.

```
evals/
  lib/common.py         shared helpers (env vars, snapshot, wait_for, node aliases)
  setup/                cluster.py (create/delete the cluster), reset.py, per-eval starting states
  verify/               deterministic checks run after the agent finishes
  skills/<skill>.json   eval cases for one skill
  agent-settings.json   permissions for the agent under test (claude -p)
  selfcheck.py          tests setup/verify scripts with no agent
```

| Skill / use case | File | Status |
|---|---|---|
| OP-06 configure node targeting | `skills/configure-node-targeting.json` | evals written; the skill is not written yet |

## Eval case format

```json
{
  "skill": "kerberator-configure-node-targeting",
  "evals": [
    {
      "id": "restrict-existing-label",
      "type": "skill",
      "prompt": "Make svc-2001's tickets only land on the prod-a security zone nodes.",
      "setup": "setup/op06-prod-a-label.py",
      "expected": ["inspects node labels before changing anything", "..."],
      "verify": "verify/node-targeting.py --nodes @w1 --selector security-zone=prod-a"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | short unique name of the case |
| `type` | *(our addition)* `skill` (one skill in isolation), `safety` (the agent should stop or ask), `diagnostic` (find a hidden cause), `e2e` (needs several steps or layers). Only used to group results. |
| `prompt` | the only thing the agent sees. It reads like a user request and never names the hidden state. |
| `setup` | command, run from `evals/`, that puts the cluster in the starting state. Optional. |
| `expected` | behaviours for the judge to check in the transcript. Positive and negative ("does not …"). |
| `verify` | command, run from `evals/`, that checks the real cluster afterwards. Exit 0 = pass. Optional. |

## The cluster

The evals run on a **disposable** kind cluster, `kerberator-eval`, with one control-plane and two workers (`kerberator-eval-worker`, `kerberator-eval-worker2`). OP-06 needs two workers to show a ticket on one node and not the other. The control-plane keeps kind's default `NoSchedule` taint, so the daemon runs only on the workers.

```bash
evals/setup/cluster.py            # create + install Kerberator, demo KDC, Tenant demo, Principal svc-2001
evals/setup/cluster.py --delete   # tear down
```

You need Docker running, plus `kind`, `kubectl`, `helm`, `jq` and `make`. `LOCAL_IMAGES=1` builds images from this checkout instead of pulling them.

The cluster's kubeconfig goes to `$EVAL_KUBECONFIG` only, not `~/.kube/config`. Every script uses it, so the evals can never touch another cluster.

### Environment variables (`lib/common.py`)

| Variable | Default | What it points to |
|---|---|---|
| `EVAL_CLUSTER` | `kerberator-eval` | the kind cluster name; node names are derived from it |
| `EVAL_STATE` | `${TMPDIR:-/tmp}/kerberator-eval` | local folder for the kubeconfig and `baseline.json` |
| `EVAL_KUBECONFIG` | `$EVAL_STATE/kubeconfig` | kubeconfig containing only the eval cluster |
| `EVAL_CONTEXT` | `kind-$EVAL_CLUSTER` | context inside that kubeconfig |
| `EVAL_NS` | `kerberator-demo` | namespace of Tenant `demo` and Principal `svc-2001` |
| `EVAL_TIMEOUT` | `300` | seconds `wait_for` polls before giving up. A selector or label change reaches each daemon only when kubelet syncs the roster ConfigMap into its pod, which took 1 to 2.5 minutes on a busy laptop, so the status can lag that long. |

### Baseline and reset

`setup/reset.py` restores the quickstart baseline. It:

- removes the node label and taint keys the evals own;
- uncordons the workers;
- `replace`s Tenant `demo` and Principal `svc-2001` from `examples/quickstart/`, which removes any selector or toleration an agent added;
- deletes extra Principals;
- makes sure the `svc-2001-keytab` Secret holds the demo KDC's **current** key, and re-exports it if not. The demo KDC keeps its database on an `emptyDir`, so if its pod is recreated (for example when Docker or the laptop sleeps and kind restarts), it creates a new realm with new keys and every mint fails with "Password incorrect". The comparison hashes `klist -kK` output inside the KDC pod, so no key material is printed;
- **recreates the daemon pods** and waits until they are Ready on both workers, `svc-2001` is Minted on both (2/2), and its ticket file is on both;
- deletes Events left by earlier daemon pods.

Recreating the pods means every eval starts the same way, with fresh mint Events and no history from the previous eval. Old prune or failure Events otherwise mislead the agent, and Principal status goes stale after 5 minutes (see [the known issue](https://github.com/dell/kerberator/pull/22)). It deletes the pods rather than running `kubectl rollout restart`, because the operator reverts the restart annotation and only one pod would restart. Running it twice changes no spec, label or taint; it only replaces the pods again.

Every setup script runs `reset.py` first. It then sets its state and polls with `wait_for` until that state is observable. If the state never appears, setup fails, so the agent never starts on a cluster that isn't in the state the case describes. Finally it saves `baseline.json`, a snapshot that contains:

- the Tenant and Principal specs;
- the DaemonSet's nodeSelector, tolerations and image;
- the name and `resourceVersion` of each user Secret. The keytab bytes are never read. A changed version is enough to prove a write.
- each node's labels, taints and cordon flag.

### Verify scripts

- `verify/no-change.py` passes if a fresh snapshot equals the baseline. Use it where the right answer is to look, explain, or ask.
- `verify/node-targeting.py` waits for the operator and daemons to settle, then checks:
  - **the ticket file** (`/tmp/krb5cc_<uid>`) exists on every node in `--nodes` and on no other node running a daemon. This is the main check. It reads the kind node containers directly with `docker exec`, so it doesn't depend on the operator's status reporting;
  - the Principal status agrees: `Minted` on exactly `--nodes` with `Ready=True n/n`. A stale status (empty reasons, see [the known issue](https://github.com/dell/kerberator/pull/22)) is accepted with a note, as long as it contradicts nothing: no other node `Minted`, no failure reason, and the right number of eligible nodes;
  - its `spec.nodeSelector` equals `--selector`;
  - optionally, daemon pods run on exactly `--tenant-nodes`, and the Tenant has `--tenant-selector`;
  - the Tenant is unchanged unless `--tenant-may-change` is given;
  - no other Principal or Secret changed;
  - node labels equal the baseline plus `--add-label`/`--remove-label`, and taints are untouched.

  Node arguments accept `@cp`, `@w1` and `@w2`.

## Running one eval by hand

No harness exists yet, so a run is manual:

```bash
export EVAL_STATE=${TMPDIR:-/tmp}/kerberator-eval
case=restrict-existing-label
c="$(jq -c --arg id "$case" '.evals[] | select(.id == $id)' evals/skills/configure-node-targeting.json)"

# 1. Starting state (fails loudly if the state can't be reached)
(cd evals && eval "$(jq -r .setup <<<"$c")")

# 2. Agent workspace: a copy WITHOUT evals/ (the expected answers), kubeconfig
#    files, or the stray root-level kubectl-krb binary. --delete-excluded also
#    removes excluded files left over from an earlier copy.
rsync -a --delete --delete-excluded --exclude evals/ --exclude '*.kubeconfig' \
  --exclude /kubectl-krb --exclude .git/ ./ "$EVAL_STATE/agent-repo/"

# 3. Run the agent with only the prompt, on only the eval cluster
(cd "$EVAL_STATE/agent-repo" && \
  KUBECONFIG="$EVAL_STATE/kubeconfig" EDITOR=false KUBE_EDITOR=false PATH="$PWD/bin:$PATH" \
  claude -p "$(jq -r .prompt <<<"$c")" \
    --settings "$OLDPWD/evals/agent-settings.json" \
    --allowedTools "Bash($PWD/bin/kubectl-krb:*)" \
      "Bash($PWD/.claude/skills/kerberator-inspect/scripts/inspect.py:*)" \
      "Bash($PWD/.claude/skills/kerberator-diagnose-tenant/scripts/diagnose.py:*)" \
    --output-format stream-json --verbose < /dev/null > "$EVAL_STATE/$case.transcript.jsonl")

# 4. Check the real cluster
(cd evals && eval "$(jq -r .verify <<<"$c")")

# 5. Grade the transcript against the expected list (by hand, or with an LLM judge)
jq -r '.expected[]' <<<"$c"
jq -r 'select(.type=="result") | .result' "$EVAL_STATE/$case.transcript.jsonl"
```

`bin/kubectl-krb` must exist for the agent to use `kubectl krb` (`make build` or the release binary).

### Agent permissions

`claude -p` cannot ask anyone for approval, so a command that isn't allowed is simply denied. `agent-settings.json` allows:

- `kubectl` and `kubectl krb`;
- the Kerberator skill scripts, in the forms the skills tell the agent to use (`cd <skill dir> && scripts/x.py`, a relative path, or an absolute path; the absolute-path forms are added with `--allowedTools` because they depend on `$EVAL_STATE`);
- read-only shell helpers (`grep`, `head`, `tail`, `cat`, `ls`, `wc`, `jq`) and the Read/Grep/Glob tools.

It denies `Edit`, `Write`, web access, and reading `evals/`, kubeconfig files or `~/.kube`. `sed` and `awk` are deliberately not allowed, because they can write files.

`EDITOR=false` makes `kubectl krb edit` fail at once instead of waiting forever on an editor nobody can use. An agent should change resources with `kubectl patch` or `apply` here.

Before grading, check the transcript's `permission_denials`. If a reasonable command was denied, the run says nothing about the agent: widen the allowlist and rerun. Two early runs failed that way. One agent called the plugin by path (`./kubectl-krb`); the other ran a skill script by absolute path.

## Checking the eval scripts themselves

`selfcheck.py` tests the setup and verify scripts with **no agent**, so you can trust a verify result before grading an agent with it. For every case:

1. setup must succeed, and `no-change.py` must PASS straight after it;
2. for cases verified by `node-targeting.py`: verify must **FAIL** before any change (nothing has been done yet), then **PASS** after the correct change is applied by hand (the `fix_<id>` functions in the script).

```bash
evals/selfcheck.py                          # all cases, about 20 minutes
evals/selfcheck.py tainted-node key-typo    # selected cases
```

Run it after changing any setup, verify or `lib/` script, and add a `fix_<id>` function for every new case that expects a change. Don't let the machine sleep during a run: the waits use the wall clock, so results after a sleep are void.
