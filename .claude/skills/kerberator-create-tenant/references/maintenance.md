# Maintainer notes

This file is for skill authors, not runtime execution. The creation workflow must work from a copied `.claude/skills` tree without repository documentation, source files, or build tools. Keep runtime facts in `SKILL.md` or its two on-demand references; do not route operators here for diagnosis.

## Compatibility facts to review when publishing

- Recheck the supported CLI flags and help against a representative installed version. Older examples describe an optional CLI image and a 720-minute renewal default; this package supports a required CLI image and a 30-minute effective renewal interval. Manifest image inheritance is intentional and distinct from the CLI requirement.
- Verify that client dry-run remains a Boolean flag, emits YAML without persistence, and may still need kubeconfig resolution. Creation must remain a straight Create with AlreadyExists handling, not an upsert.
- Preserve validation parity between CLI and manifest creation, including uppercase realm shape, exact realm and `[realms]` checks for supplied configuration, image-reference shape, and positive effective renewal interval. A CLI dry-run cannot validate or preserve advanced fields it cannot represent.
- Verify installed API fields/defaults, the default ServiceAccount and Event RBAC, child naming and ownership, cache-path/readiness behavior, and security constraints before changing the recorded facts.
- Keep the Principal admission dependency distinct from Tenant creation: the supported webhook validates Principals and checks their parent Tenant. Do not infer a Tenant webhook from old CLI commentary.
- Recheck describe/list output limitations: convention-only child names, hidden Principal-list errors, and dangling references omitted by grouped list output. Status summaries must not be treated as proof of KDC authentication.

## Packaging and review

Package `SKILL.md` and the `references/` directory together. Load the two runtime references only for their documented decision points; this maintenance file is for authors only. Diagnosis and registration hand-offs are optional sibling skills, checked only when selected; missing destinations must yield a named hand-off and evidence, never a source lookup or improvised diagnosis. Keep the requested DG-01, DG-02, and OP-03 routing labels associated with their skill names when reviewing destination metadata.

Validate the frontmatter, local reference links, source independence, and whitespace after changes. Review scenarios covering a manifest with lowercase realm or mismatched configuration, missing CLI with intentional image inheritance, a creation timeout followed by read-back, incomplete readiness despite Ready=True, mint failures, and absent hand-off destinations. Do not use live cluster mutations merely to validate documentation edits.
