#!/usr/bin/env python3
"""Create local YAML fixtures for OP-08 evaluation cases.

Usage:
  python setup.py <state>

This script writes only evaluator-owned fixture files under the system temp
directory. It does not contact or modify Kubernetes. The printed FIXTURE_DIR
is substituted into the case prompt by the evaluator/operator.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

RUN_DIR = Path(tempfile.gettempdir()) / "kerberator-op08-validate-config"
FIXTURE_DIR = RUN_DIR / "fixtures"
MANIFEST = RUN_DIR / "baseline.json"
NAMESPACE = "kerberator-demo"

TENANT_BASE = """apiVersion: kerberator.dell.com/v1alpha1
kind: Tenant
metadata:
  name: eval-tenant
  namespace: kerberator-demo
spec:
  realm: EXAMPLE.COM
  daemon:
    krb5Conf: |
      [libdefaults]
        default_realm = EXAMPLE.COM
      [realms]
        EXAMPLE.COM = {
          kdc = kdc.kerberator-demo.svc.cluster.local
        }
"""

PRINCIPAL_BASE = """apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata:
  name: eval-principal
  namespace: kerberator-demo
spec:
  uid: 2001
  principal: svc-2001@EXAMPLE.COM
  tenantRef:
    name: eval-tenant
  keytabSecretRef:
    name: svc-2001-keytab
    key: keytab
"""

FIXTURES = {
    "valid-tenant": {
        "valid-tenant.yaml": TENANT_BASE,
    },
    "invalid-tenant-realm": {
        "invalid-tenant-realm.yaml": TENANT_BASE.replace(
            "realm: EXAMPLE.COM", "realm: example.com"
        ),
    },
    "invalid-principal-uid": {
        "invalid-principal-uid.yaml": PRINCIPAL_BASE.replace("uid: 2001", "uid: true"),
    },
    "empty-node-selector": {
        "empty-node-selector.yaml": PRINCIPAL_BASE + "  nodeSelector: {}\n",
    },
    "malformed-yaml": {
        "malformed.yaml": (
            "apiVersion: kerberator.dell.com/v1alpha1\n"
            "kind: Tenant\n"
            "metadata:\n"
            "  name: [unterminated\n"
        ),
    },
    "valid-with-warnings": {
        "tenant-image-warning.yaml": TENANT_BASE.replace(
            "    krb5Conf:",
            "    image: invalid-image-reference\n    krb5Conf:",
        ),
    },
    "principal-reference": {
        "principal-reference.yaml": PRINCIPAL_BASE,
    },
}

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in FIXTURES:
        print(f"usage: {sys.argv[0]} <{'|'.join(FIXTURES)}>", file=sys.stderr)
        return 2

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    # Remove only files previously generated in this dedicated evaluator dir.
    for old in FIXTURE_DIR.glob("*.yaml"):
        old.unlink()

    for filename, content in FIXTURES[sys.argv[1]].items():
        path = FIXTURE_DIR / filename
        path.write_text(content, encoding="utf-8")

    baseline = {
        "state": sys.argv[1],
        "fixture_dir": str(FIXTURE_DIR),
        "files": {
            path.name: sha256(path)
            for path in sorted(FIXTURE_DIR.glob("*.yaml"))
        },
    }
    MANIFEST.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    print(f"STATE: {sys.argv[1]}")
    print(f"FIXTURE_DIR: {FIXTURE_DIR}")
    print(f"BASELINE: {MANIFEST}")
    for name in baseline["files"]:
        print(f"FIXTURE: {FIXTURE_DIR / name}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
