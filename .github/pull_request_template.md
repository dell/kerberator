<!--
Thanks for contributing. A few things before you submit:

1. Every commit must be signed off with `git commit -s` (see CONTRIBUTING.md § DCO).
2. CI must be green — `make vet`, `make test`, `make integration` must all pass.
3. New user-visible behavior needs a docs update and a CHANGELOG entry.
-->

## What does this PR do?

A one-paragraph summary. Which component (operator / daemon / cli / shared / docs / chart)? What behavior changes?

## Why?

Link the Issue this closes (`Fixes #NNN`) or explain the motivation if there's no filed Issue.

## How was this tested?

- [ ] Unit tests added or updated
- [ ] `make test` passes locally
- [ ] `make integration` passes locally (envtest)
- [ ] Manually verified against a live cluster (describe how)

## Breaking changes

- [ ] No breaking changes
- [ ] Breaking API change (CRD schema, admission webhook rule, CLI flag) — CHANGELOG updated with migration notes

## Docs

- [ ] Docs updated (`README.md`, `docs/usage.md`, or relevant per-component README)
- [ ] `CHANGELOG.md` entry added under the next unreleased version heading
- [ ] N/A (this PR has no user-visible change)

## Checklist

- [ ] Commits are signed off (`git commit -s`)
- [ ] Code is formatted with `gofmt -s`
- [ ] `make vet` passes
- [ ] Any new source files carry the SPDX header
