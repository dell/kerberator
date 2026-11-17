#!/usr/bin/env python3
"""Read-only preflight checks for a Kerberator installation or upgrade."""

import argparse
import json
import subprocess
import sys


def run(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "error": result.stderr.strip() or f"Exit code {result.returncode}",
            }
        return {"ok": True, "output": result.stdout}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}


def parse_json(result):
    if not result["ok"]:
        return result
    try:
        return {"ok": True, "data": json.loads(result["output"])}
    except json.JSONDecodeError:
        return {"ok": False, "error": "Command did not return valid JSON"}


def main():
    parser = argparse.ArgumentParser(
        description="Collect read-only Kerberator installation preflight information."
    )
    parser.add_argument(
        "--context",
        required=True,
        help="Explicit Kubernetes context to inspect",
    )
    args = parser.parse_args()

    context = args.context
    checks = {}

    checks["kubernetes_version"] = parse_json(
        run(["kubectl", "--context", context, "version", "-o", "json"])
    )

    checks["helm_releases"] = parse_json(
        run(["helm", "--kube-context", context, "list", "-A", "-o", "json"])
    )

    checks["kerberator_workloads"] = parse_json(
        run([
            "kubectl", "--context", context, "get",
            "deployments,pods", "-A",
            "-l", "app.kubernetes.io/name=kerberator",
            "-o", "json",
        ])
    )

    checks["tenant_crd"] = parse_json(
        run([
            "kubectl", "--context", context, "get",
            "crd", "tenants.kerberator.dell.com", "-o", "json",
        ])
    )

    checks["principal_crd"] = parse_json(
        run([
            "kubectl", "--context", context, "get",
            "crd", "principals.kerberator.dell.com", "-o", "json",
        ])
    )

    checks["cli_version"] = run(
        ["kubectl", "--context", context, "krb", "version"]
    )

    report = {
        "context": context,
        "checks": {},
    }

    for name, result in checks.items():
        if not result["ok"]:
            report["checks"][name] = {
                "ok": False,
                "error": result["error"],
            }
            continue

        if "data" in result:
            data = result["data"]

            if name == "kubernetes_version":
                report["checks"][name] = {
                    "ok": True,
                    "client_version": data.get("clientVersion"),
                    "server_version": data.get("serverVersion"),
                }

            elif name == "helm_releases":
                report["checks"][name] = {
                    "ok": True,
                    "releases": [
                        {
                            key: release.get(key)
                            for key in (
                                "name", "namespace", "revision",
                                "status", "chart", "app_version",
                            )
                        }
                        for release in data
                    ],
                }

            elif name == "kerberator_workloads":
                workloads = []
                for item in data.get("items", []):
                    metadata = item.get("metadata", {})
                    spec = item.get("spec", {})
                    status = item.get("status", {})
                    workloads.append({
                        "kind": item.get("kind"),
                        "name": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "phase": status.get("phase"),
                        "ready_replicas": status.get("readyReplicas"),
                        "available_replicas": status.get("availableReplicas"),
                        "containers": [
                            {
                                "name": container.get("name"),
                                "image": container.get("image"),
                            }
                            for container in (
                                spec.get("containers", [])
                                or spec.get("template", {}).get(
                                    "spec", {}
                                ).get("containers", [])
                            )
                        ],
                    })
                report["checks"][name] = {
                    "ok": True,
                    "workloads": workloads,
                }

            else:
                report["checks"][name] = {
                    "ok": True,
                    "name": data.get("metadata", {}).get("name"),
                    "versions": [
                        {
                            "name": version.get("name"),
                            "served": version.get("served"),
                            "storage": version.get("storage"),
                        }
                        for version in data.get("spec", {}).get("versions", [])
                    ],
                }
        else:
            report["checks"][name] = {
                "ok": True,
                "version": result["output"].strip(),
            }

    report["all_checks_succeeded"] = all(
        result["ok"] for result in report["checks"].values()
    )

    print(json.dumps(report, indent=2))
    return 0 if report["all_checks_succeeded"] else 1


if __name__ == "__main__":
    sys.exit(main())
