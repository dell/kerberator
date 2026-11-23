#!/usr/bin/env python3
"""Create synthetic, non-sensitive OP-03 evaluation fixtures."""
import argparse
import json
from pathlib import Path

FIXTURES = {
    "ready-local-keytab": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "principal": {"uid": "2004", "name": "uid-2004", "exists": False},
        "keytab": {"source": "local-file", "exists": True, "readable": True,
                   "principal_matches": True},
        "permissions": {"list_principals": True, "create_principal": True,
                       "create_secret": True},
        "expected_precheck": "READY"
    },
    "duplicate-principal": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "principal": {"uid": "2001", "name": "uid-2001", "exists": True},
        "expected_precheck": "BLOCKED"
    },
    "principal-list-forbidden": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "permissions": {"list_principals": False},
        "uniqueness": "unknown",
        "expected_precheck": "BLOCKED"
    },
    "wrong-realm": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "requested_principal": "svc-2004@OTHER.COM",
        "expected_precheck": "BLOCKED"
    },
    "missing-keytab": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "keytab": {"source": "local-file", "exists": False, "readable": False},
        "expected_precheck": "BLOCKED"
    },
    "existing-secret": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "secret": {"name": "approved-keytab", "key": "2004.keytab",
                   "exists": True, "key_exists": True},
        "secret_data_included": False,
        "identity_and_kvno_verified": False,
        "expected_precheck": "READY WITH WARNINGS"
    },
    "partial-registration": {
        "simulated_result": {
            "secret_created": True,
            "principal_created": False,
            "error": "simulated Principal creation failure"
        },
        "expected_action": "STOP; report partial state; no retry or cleanup"
    },
    "principal-not-healthy": {
        "simulated_result": {
            "principal_exists": True,
            "ready": False,
            "node_status": [
                {"node": "node-a", "reason": "Minted"},
                {"node": "node-b", "reason": "MintFailed"}
            ]
        },
        "expected_outcome": "REGISTERED BUT NOT HEALTHY"
    }
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True,
                        help="Directory where synthetic fixtures will be written")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "suite": "op03-register-principal",
        "fixture_mode": "synthetic-only",
        "live_cluster_access": False,
        "contains_real_credentials": False,
        "fixtures": sorted(FIXTURES)
    }

    for name, contents in FIXTURES.items():
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")

    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Created {len(FIXTURES)} synthetic fixtures in {args.out}")


if __name__ == "__main__":
    main()
