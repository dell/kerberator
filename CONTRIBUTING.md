# Contributing to Kerberator

Thanks for your interest in contributing. This document covers the mechanics
of getting a patch reviewed and merged. For the higher-level "why does this
project exist and what is it for" question, see the [README](README.md) and
[docs/](docs/).

## TL;DR

1. Fork the repo on GitHub, create a topic branch (`fix/…` or `feat/…`).
2. Make your change with tests. Keep commits focused and self-explanatory.
3. Sign every commit off with `git commit -s` (see [Developer Certificate of
   Origin](#developer-certificate-of-origin) below — required).
4. Push the branch to your fork, open a Pull Request against `main`.
5. CI must be green and one maintainer must approve before merge.

## Developer Certificate of Origin

Contributions are accepted under the terms of the [Developer Certificate of
Origin](https://developercertificate.org/) (DCO). To signal your acceptance,
sign every commit off with the `--signoff` (`-s`) flag:

```
git commit -s -m "feat(operator): add SPIFFE identity source"
```

This appends a `Signed-off-by:` trailer to the commit message using your
`user.name` / `user.email`. Commits without a valid sign-off will fail CI.

We do **not** require a separate CLA.

## Coding style

### Go

- Format with `gofmt` (`make fmt`). CI runs `go vet` and `golangci-lint`
  (`make lint`) and rejects failures.
- Prefer smaller, focused changes. If a PR grows beyond ~500 lines of diff,
  consider splitting.
- Public APIs (exported types, functions, and CRD schema fields) need
  Go-style doc comments starting with the identifier's name.
- Add tests for new behavior. `internal/…` packages should have `_test.go`
  neighbors.
- The repo is a single Go module (`github.com/dell/kerberator`) with four
  top-level directories: `operator/`, `daemon/`, `cli/kubectl-krb/`, and
  `shared/`. Keep `shared/` free of Kubernetes and TUI imports; it is what
  the daemon and the operator agree on.

### Kubernetes API and CRD changes

- Any change to `operator/api/v1alpha1/*_types.go` needs a corresponding
  update to the CRD manifests under `operator/charts/kerberator/crd/`.
- The API is `v1alpha1`. Breaking changes are avoided when possible; when
  unavoidable, they need a `### Breaking` entry in `CHANGELOG.md` and a
  migration note.
- Admission webhook logic lives in `operator/internal/webhook/`; each new
  validation rule needs a table-driven test.

### Docs

- Every user-visible feature or CLI subcommand needs a corresponding docs
  entry in `docs/usage.md` and (if relevant) the top-level `README.md`.
- The CHANGELOG is loosely [Keep a Changelog](https://keepachangelog.com/)
  style. Add an entry under the next unreleased version heading.

## Building and testing locally

Requires Go (the version in `go.mod`), `helm`, and for images a container
tool (`docker` by default; `CONTAINER_TOOL=buildah` or `podman` also work).

```bash
make build         # operator, daemon, kubectl-krb into ./bin
make test          # unit tests
make vet lint      # go vet + golangci-lint (downloads the linter into ./bin)
make integration   # operator envtest suite (downloads envtest assets into ./bin)
make check         # vet + test + integration + helm lint
```

For iterative development against a live cluster:

```bash
# Local kind cluster with cert-manager, Kerberator, an in-cluster demo KDC,
# and one Principal. Uses published images by default; LOCAL_IMAGES=1 builds
# and loads your working tree instead.
make quickstart LOCAL_IMAGES=1

make quickstart-clean
```

`make help` lists every target.

## Releasing

Maintainers only. `make release-prep VERSION=x.y.z` bumps the chart version
and appVersion. Commit that, tag `vx.y.z`, push the tag. The release workflow
builds multi-arch images, pushes the chart as an OCI artifact, renders
`install.yaml`, builds `kubectl-krb` binaries, and publishes the GitHub
Release.

## Reporting bugs and requesting features

- **Bugs:** open a GitHub Issue using the "Bug report" template. Include
  the version (`kubectl krb version`), the operator log excerpt showing
  the failure, and the minimum reproducible steps.
- **Feature requests:** open an Issue using the "Feature request" template
  and describe the use case first. Small, focused proposals move faster
  than large designs.

## Security disclosures

**Do NOT open a public GitHub Issue for security vulnerabilities.** See
[SECURITY.md](SECURITY.md) for the responsible-disclosure process.

## Code of conduct

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

Kerberator is released under the MIT License. See [LICENSE](LICENSE). By
contributing you agree that your contribution will be licensed under the
same terms.
