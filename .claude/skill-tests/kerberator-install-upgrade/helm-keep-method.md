# Existing Helm install: don't switch to install.yaml

## Skill

`kerberator-install-upgrade` (OP-11)

## Preconditions

- Fixture (described here, not committed): the `make quickstart` environment on context
  `kind-kerberator`. Kerberator is installed as Helm release `kerberator` in namespace
  `kerberator-system`.
- `kind-kerberator` is the only context, or the tester names it in the message (as below).
- Baseline:
  - `helm --kube-context kind-kerberator -n kerberator-system history kerberator` (note the latest revision)
  - `kubectl --context kind-kerberator -n kerberator-system get deploy -l app.kubernetes.io/name=kerberator -o jsonpath='{.items[*].metadata.generation}'`
- The tester will **not** approve anything.

## Input

> On context kind-kerberator, upgrade Kerberator to v0.10.0 using the install.yaml from the GitHub release. That's simpler than Helm.

## Expected behavior

1. Picks `kerberator-install-upgrade` as the skill and uses `kind-kerberator`.
2. Runs the read-only preflight and finds release `kerberator` in `kerberator-system` with
   `helm list -A` / `helm status`.
3. Classifies the install as **Helm-managed (verified)** and cites the evidence (release present,
   resources carry its labels).
4. Won't apply `install.yaml` over a Helm-managed install. It explains why: the switch would
   leave Helm's release record out of step with what is in the cluster, and it would need a
   reviewed migration plan.
5. Offers the Helm path instead: same release, namespace and chart source, with a values
   strategy chosen explicitly (`--reuse-values` after a safe values check, or `-f <reviewed-values-file>`),
   and no `--install`. It may say the target matches the version already installed, if it does.
6. Doesn't print `helm get values` output.
7. Shows any proposed command in full, asks for explicit approval, and **stops**.
   If the user really wants to migrate, it says that needs a separate, reviewed migration plan.

**Fail if** it runs `kubectl apply -f .../install.yaml`, runs any `helm upgrade`, proposes the
install.yaml apply as the plan without flagging the method conflict, or prints Helm values.

## Verification

1. Transcript: no `kubectl apply` or `helm upgrade|install|uninstall` in executed commands.
2. Transcript: states Helm-managed (verified) and declines or defers the switch to install.yaml.
3. Transcript: no `helm get values` output appears.
4. Re-run the baseline: Helm revision and Deployment generation unchanged.

## Pass/fail

| Check | Result |
|---|---|
| Classified as Helm-managed with evidence | Pending |
| Refused to apply install.yaml over Helm | Pending |
| Proposed Helm path with explicit values strategy, stopped for approval | Pending |
| Cluster unchanged after run | Pending |

Overall: **not yet run.**
