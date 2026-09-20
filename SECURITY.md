# Security Policy

## Supported versions

Kerberator is currently in early development (v0.x). Only the latest tagged
release receives security fixes. Once we hit v1.0 we'll extend the support
window and document it here.

## Reporting a vulnerability

**Please do not open a public GitHub Issue for security vulnerabilities.**
Instead, report privately so we can coordinate a fix and disclosure.

### Preferred channel

Email **security@dell.com**, or use GitHub's private vulnerability reporting
on this repository if it is enabled. Include:

- A clear description of the vulnerability, including the specific
  component (`operator`, `daemon`, `kubectl-krb`, or the shared keytab
  parser).
- Reproduction steps, or at minimum enough detail for us to reproduce
  independently.
- The version affected (`kubectl krb version` output plus operator +
  daemon image tags if known).
- Your assessment of impact (information disclosure, credential leak,
  privilege escalation, denial of service, etc.).

### Response expectations

- **Acknowledgement:** within 3 business days.
- **Initial assessment:** within 7 business days, including CVSS score
  and rough timeline.
- **Fix:** severity-dependent. Critical vulnerabilities aim for a patch
  release within 30 days; lower severity issues may be batched into the
  next planned release.
- **Public disclosure:** coordinated with the reporter. We prefer a
  90-day window from initial report to public disclosure, extendable if
  a fix is genuinely in flight and the reporter agrees.

We follow the [Dell Product Security Response Policy](https://www.dell.com/support/kbdoc/en-us/000180119/dell-vulnerability-response-policy)
for CVE assignment and advisory publication.

## What counts as a security issue

Examples of things we treat as security vulnerabilities:

- Kerberos credentials (keytabs or ccaches) being exposed outside their
  intended UID or reader boundary.
- The daemon or operator escalating privileges beyond what's declared in
  the Helm chart RBAC / PodSecurityContext.
- The admission webhook accepting a malformed Principal resource that leads
  to a ccache being minted for the wrong UID.
- Any way for a non-admin K8s user to read another tenant's keytab or
  ccache material.
- Path-traversal or symlink attacks against on-node ccache paths.
- Denial-of-service against the operator that cascades to daemon
  unavailability across the fleet.

Examples of things that are **not** security issues (please open regular
Issues for these):

- General bugs that don't cross a security boundary.
- Performance regressions.
- Documentation errors.
- CVEs in transitive Go dependencies where Kerberator itself is not
  affected (please still tell us — we'll bump the dep — but a
  non-exploitable-in-Kerberator CVE isn't itself a Kerberator vuln).

## Hardening recommendations

See [`docs/security-model.md`](docs/security-model.md) for the full
security model, including the trust boundaries the daemon and operator
sit across, and the recommended admission-policy posture for clusters
running Kerberator.
