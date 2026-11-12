# Kerberator agent skills

Agent skills for operating and diagnosing Kerberator with `kubectl krb`,
and the tests for them. Each skill is a folder with a `SKILL.md`; agents
(Claude Code, Copilot) load it when a request matches its description.

```
.claude/
├── README.md                 this index
├── skills/<skill-name>/      one folder per skill
│   ├── SKILL.md              instructions; the description decides when it's used
│   ├── scripts/              Python helpers the skill runs (read-only checks, guarded actions)
│   ├── references/           detail read only when needed
│   └── tests/                unit tests for the scripts (no cluster needed)
└── skill-tests/<skill-name>/ agent evals for that skill (location under review, see below)
```

## Skills

Status as of 2026-11-11. PR numbers are on `dell/kerberator`, branch `skills`.

### Operations

| Story | Skill | Status |
|---|---|---|
| OP-01 Inspect Tenants and Principals | [`kerberator-inspect`](skills/kerberator-inspect/) | Merged; Python conversion in #23 |
| OP-02 Create a Tenant | [`kerberator-create-tenant`](skills/kerberator-create-tenant/) | Merged (#20); fixes in #30 |
| OP-03 Register a Principal | [`kerberator-register-principal`](skills/kerberator-register-principal/) | Merged (#19); known bugs, fix pending |
| OP-04 Rotate a keytab | [`kerberator-rotate-keytab`](skills/kerberator-rotate-keytab/) | Merged |
| OP-05 Enable or disable a Principal | [`kerberator-enable-disable-principal`](skills/kerberator-enable-disable-principal/) | Merged |
| OP-06 Configure node targeting | [`kerberator-configure-node-targeting`](skills/kerberator-configure-node-targeting/) | Merged (#18) |
| OP-07 Drain a node | [`kerberator-drain-node`](skills/kerberator-drain-node/) | Merged (draft) |
| OP-08 Validate a Tenant/Principal YAML | `kerberator-validate-config` | PR #32 |
| OP-09 Update a Tenant or Principal | [`kerberator-update-tenant-principal`](skills/kerberator-update-tenant-principal/) | Merged |
| OP-10 Remove a Principal or Tenant | [`kerberator-remove-principal-or-tenant`](skills/kerberator-remove-principal-or-tenant/) | Merged (#29) |
| OP-11 Install or upgrade Kerberator | `kerberator-install-upgrade` | PR #28; tests in #31 |

### Diagnostics

| Story | Skill | Covers | Status |
|---|---|---|---|
| DG-01 Tenant not Ready | `kerberator-diagnose-tenant` | Tenant status, events and config; node targeting problems (was DG-05); each Tenant's daemon | PR #24 |
| DG-02 Principal / authentication failure | [`kerberator-diagnose-auth-failure`](skills/kerberator-diagnose-auth-failure/) | Principal not Ready, keytab/KVNO mismatch (was DG-03), missing or stale node tickets (was DG-04), Kerberos login errors (was DG-06). One reference file per cause | Merged as DG-06; DG-02 merge in progress |
| DG-07 Interpret Kerberator events | not built | What events do and don't establish | Not started |
| DG-08 Operator health | not built | The operator controller only: its pod, logs, installed CRDs, version mismatch | Not started |

**Merged stories:** DG-03, DG-04 and DG-06 → DG-02. DG-05 → DG-01 (OP-06
makes the fix). The operator's per-Tenant daemon (from DG-08) → DG-01.

## Conventions

- **Names:** `kerberator-<verb>-<object>`. The folder name and the `name:` in
  SKILL.md must match. Put the story ID in the SKILL.md title, e.g.
  `# Rotate a keytab (OP-04)`.
- **Scripts are Python** 3.8+, standard library only (no `jq`, no `pip`),
  run as `python3 <skill-dir>/scripts/<name>.py`. Parse `kubectl ... -o json`
  rather than text. Put unit tests in the skill's `tests/` folder; they must
  run without a cluster.
- **`kubectl krb`: subcommand first, then flags.** `kubectl krb list --context C -n NS`
  works; `kubectl --context C krb -n NS list` fails with "flags cannot be
  placed before plugin name".
- **Self-contained:** a skill only uses files in its own folder, so it can be
  copied anywhere. To build on another skill, name it as a hand-off; don't
  read or run its files.
- **Safety:** read-only by default. Anything that changes the cluster shows
  the exact command and waits for the user's explicit approval. Never read
  Secret or keytab contents: check a Secret exists with `-o name`, or list its
  keys with a go-template that prints key names only. No `git commit`/`git push`
  from a skill.
- **Agent evals** are kept apart from the skill folders, so an agent using a
  skill can't read the expected answers. Today they live in
  `skill-tests/<skill-name>/`; moving them to a root `evals/` folder, as in the
  team's testing process document, is under discussion (#25).
- **Branches and PRs:** branch from `skills` as `<github-user>/<story>-<short-name>`,
  open the PR into `skills`, signed-off commits (`git commit -s`), at least one
  review before merging. PRs that touch `.claude/` request the skills team
  automatically (`.github/CODEOWNERS`). Squash and merge, then delete the branch.
- **Credentials:** never commit keytabs, kubeconfigs, ticket caches or
  `__pycache__` (`.gitignore` blocks `*.keytab`, `keytabs/`, `*.kubeconfig`, `krb5cc_*`, `__pycache__/`).
