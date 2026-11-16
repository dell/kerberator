#!/usr/bin/env bash

die() {
  echo "error: $*" >&2
  exit 2
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
RUN_DIR="$HERE/runs"

guard_env() {
  command -v kubectl >/dev/null || die "kubectl not found"
  command -v jq >/dev/null || die "jq not found"

  if [[ -n ${KUBECONFIG:-} ]]; then
    [[ -r "$KUBECONFIG" ]] || die "KUBECONFIG is not readable"
  elif [[ ! -r "$HOME/.kube/config" ]]; then
    die "no kubeconfig found"
  fi
}

step() {
  echo
  echo "== $* =="
}