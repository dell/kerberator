---
name: kerberator-register-principal
description: Register a new Kerberator Principal (OP-03), binding a positive Linux UID to a Kerberos principal, an existing Tenant, and a keytab Secret. Use when asked to add, create, onboard, or register a Principal or service identity. Perform read-only prechecks, obtain explicit approval, create through the supported interface, and independently verify the result.
---

# Register a Kerberator Principal (OP-03)

A **Principal** maps a Linux UID to a Kerberos identity within one Kerberator Tenant. Its `spec.tenantRef.name` is the name of the Tenant custom resource, **not** the Kerberos realm. Its `spec.keytabSecretRef` identifies the Kubernetes Secret name and data key containing the binary keytab; the Secret and Principal must be in the same namespace.

## Scope and safe defaults

- Register **one new** Principal. Do not update, rotate, delete, disable, or replace existing Principals or Secrets.
- Prefer the supported `kubectl krb add-principal` command for a **local keytab file**: it creates a Secret and a Principal CR.
- When the user has an **existing Kubernetes Secret** to reference, use the declarative Principal manifest procedure below, without reading or modifying Secret data.
- Both procedures mutate cluster state. The request to register is **not** approval to execute the operation. Obtain separate, explicit approval after showing the exact action and targets.
- Never disclose keytab content, base64 values, passwords, kubeconfig data, or Secret `.data`. Never use `cat` or `klist -K` on a keytab. Do **not** use `kubectl krb add-principal --dry-run`: the current CLI prints base64-encoded keytab bytes in its manifest preview.
- Do not create a keytab, modify the KDC, or attempt an alternate creation method after a failure without a new approval. Never commit or push automatically.

## Inputs

| Input | Example | Requirement |
|---|---|---|
| Cluster/context | `dev-cluster` | User-selected if multiple contexts; state the context used |
| Namespace | `kerberator-demo` | Namespace containing the Tenant and new Principal |
| Tenant CR name | `demo` | Must already exist in that namespace |
| UID | `2003` | Positive integer, not zero; check for duplicates |
| Kerberos principal | `svc-2003@EXAMPLE.COM` | Explicit user choice, or propose `svc-<uid>@<Tenant.spec.realm>` and confirm |
| Keytab source | `./svc-2003.keytab` or `existing-secret/key` | User-provided local path OR existing Secret name/data key |
| Node selector | `security-zone=prod-a` | Optional; repeated selectors have AND semantics |

Ask only for missing essential inputs. Do not guess a namespace, UID, context, file path, Secret name, or Kerberos identity. The CLI defaults the Kerberos identity to `svc-<uid>@<realm>`; make this default visible before approval.

## 1. Precheck (read-only)

1. Establish the intended cluster context. Use `kubectl config get-contexts` if ambiguity exists; do not silently choose among multiple contexts.
2. Check that the Tenant exists and inspect its realm:

   ```bash
   kubectl --context <context> -n <namespace> get tenant <tenant> -o jsonpath='{.spec.realm}'
   ```

3. Validate that UID is a positive integer; the Kerberos identity has exactly one `@` with nonempty sides; and the realm after `@` exactly matches `Tenant.spec.realm`. If the namespace has an OpenShift `openshift.io/sa.scc.uid-range` annotation, ensure UID is within that allowed range.
4. Inspect Principal CRs **in the target namespace** for a matching UID, Kerberos identity, or intended resource name. A failed or forbidden list is **unknown**, not evidence of absence. Stop if uniqueness cannot be established. The CLI-generated resource name is `uid-<uid>`.

   ```bash
   kubectl --context <context> -n <namespace> get principals -o custom-columns=NAME:.metadata.name,UID:.spec.uid,KERBEROS:.spec.principal,TENANT:.spec.tenantRef.name
   ```

5. Check permissions for `create` on `principals` and, for the CLI/local-keytab path, `secrets`. Check whether the expected Secret (`uid-<uid>-keytab`) or Principal (`uid-<uid>`) already exists. If either exists, **stop** rather than assuming it is safe to reuse; the CLI can treat AlreadyExists as success and leave a mismatched resource untouched.
6. **Local file path:** confirm the file exists and is nonempty without printing its bytes. If `klist` is installed, inspect only identity and KVNO metadata using `klist -k -t <file>` (never `-K`); require the expected identity to be present. If metadata cannot be inspected, disclose that validation is incomplete and ask before proceeding. Check file path locally; do not download or synthesize a keytab.
7. **Existing Secret path:** verify the Secret exists in the namespace and has the specified data key, without retrieving the key's value. Read only metadata / data-key names using a suitable JSONPath projection; do not output `.data` or `.stringData`. Verify the intended Principal CR name does not exist. Do not attempt to infer the keytab's principal/KVNO from a Secret you cannot safely inspect.
8. If node selectors were requested, validate their `key=value` syntax and matching node-label inventory. Explain that an empty selector targets all Tenant-served nodes.

Report findings as `READY`, `READY WITH WARNINGS`, or `BLOCKED`. A conflict, missing Tenant, wrong realm, missing keytab, insufficient permissions, or uncertain duplicate state is `BLOCKED`.

## 2. Request explicit approval

Show context, namespace, Tenant, UID, Kerberos identity, keytab source **path or Secret reference only**, expected object names, optional selectors, and the exact proposed creation command or manifest. Explain that registration will introduce a credential-backed identity into the Tenant roster; the operator and daemon then attempt to mint credentials on the applicable nodes.

Wait for a clear affirmative response to **this specific creation**. Do not take the initial user request as permission to execute. If inputs change, repeat precheck and request approval again.

## 3A. Register using a local keytab (preferred CLI path)

Use the approved exact values:

```bash
kubectl krb -n <namespace> add-principal <tenant> <uid> \
  --keytab-file <local-keytab-path> \
  --principal <name@REALM>
```

Add `--node-selector key=value` once per approved selector, if needed. The command creates the `uid-<uid>-keytab` Secret with the `uid-<uid>.keytab` data key, followed by the `uid-<uid>` Principal CR. Execute once only. Do not use `--dry-run` because it exposes encoded keytab data in the preview.

If the command fails, stop. In particular, if it partially created a Secret before failing to create the Principal, report the orphaned Secret by **name**; do not automatically retry, patch, delete, or clean it up.

## 3B. Register referencing an existing Secret (declarative path)

Use this path **only** when the user supplied an existing Secret and its data key, and the precheck confirms its presence. Propose a unique Principal CR name, such as `svc-2003`, and show this exact manifest with user-approved values:

```yaml
apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata:
  name: svc-2003
  namespace: kerberator-demo
spec:
  uid: 2003
  principal: svc-2003@EXAMPLE.COM
  tenantRef:
    name: demo
  keytabSecretRef:
    name: svc-2003-keytab
    key: svc-2003.keytab
```

After explicit approval, create the **Principal only** from the approved manifest with `kubectl --context <context> -n <namespace> create -f <approved-manifest-path>`. For node selectors, include approved `spec.nodeSelector` keys. Do not create, inspect contents of, or replace the existing Secret.

## 4. Verify independently (read-only)

A successful CLI exit or API creation is **registration**, not proof that credential minting works. Independently check the observed resource:

```bash
kubectl --context <context> -n <namespace> get principal <name> -o jsonpath='{.spec.uid}{"\n"}{.spec.principal}{"\n"}{.spec.tenantRef.name}{"\n"}{.spec.keytabSecretRef.name}{"/"}{.spec.keytabSecretRef.key}{"\n"}'
kubectl --context <context> krb -n <namespace> describe principal <name>
kubectl --context <context> krb -n <namespace> list
```

Verify UID, identity, Tenant reference, Secret name/key, and any selectors against the approved inputs. For the local-file path, verify that the expected Secret exists **without reading its contents**. Check `Ready` and the per-node reasons.If reconciliation is still progressing, allow up to 180 seconds for verification, checking every 15 seconds. Stop early if Ready=True, all expected nodes report Minted, and Tenant coverage is complete. If the timeout expires, report REGISTERED, NOT YET VERIFIED HEALTHY with the latest observed status. Do not loop indefinitely or change resources to force readiness.

`READY=True`, expected node coverage, and `Minted` on each applicable node support a healthy result. Otherwise report `REGISTERED, NOT YET VERIFIED HEALTHY` or `FAILED`, citing actual status and events. Do not promise that all workloads already have valid tickets just because the CR exists.

## Report

Summarize separately:

- **Confirmed:** context, namespace, Principal CR name, UID, Kerberos identity, Tenant reference, Secret **reference only**, exact resources created, and actual Ready/node status.
- **Not confirmed / next steps:** warnings, failed or pending nodes, errors, missing observations, and non-destructive follow-up suggestions.

## Failure handling

| Condition | Response |
|---|---|
| Tenant absent / realm mismatch | Stop; ask for a valid existing Tenant or corrected identity |
| Existing UID / Principal / generated Secret | Stop; identify the conflict; never overwrite or silently reuse |
| Keytab missing / empty / wrong identity | Stop; request a correct file, without displaying contents |
| Existing Secret or data key missing | Stop; request the correct Secret reference |
| Forbidden / context unclear | Stop; name missing permission or unresolved context |
| CLI created Secret but Principal creation failed | Report partial creation; no automatic cleanup or retry |
| Principal created, Ready=False/Unknown | Report registration separately from minting; inspect events and refer to diagnostics skill when appropriate |
| Node selector matches no applicable nodes | Warn before approval; never claim successful minting |

## Never

- Reveal keytab bytes (including encoded base64) or Secret values.
- Run `kubectl krb add-principal --dry-run`, `kubectl get secret -o yaml/json`, `kubectl describe secret`, `kubectl get secret ... -o jsonpath='{.data...}'`, `klist -K`, or an arbitrary secret-decoding command.
- Bypass authorization, invent inputs, create KDC credentials, or modify existing identities as a workaround.
- Treat an AlreadyExists response as proof that an existing resource matches the request.
- Delete, rotate, restart DaemonSets, or change a Tenant from this skill.
- Run `git commit` or `git push`.

## Out of scope

Tenant creation, keytab generation or rotation (OP-04), enable/disable operations (OP-05), and root-cause diagnosis of failed minting (use the diagnostic skills). This skill only registers new Principal identities and verifies the observed outcome.
