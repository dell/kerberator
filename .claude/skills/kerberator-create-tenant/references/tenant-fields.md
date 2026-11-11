# Tenant fields and defaults

Read this reference only when selecting or explaining individual Tenant fields. Core creation checks and approval gates are in the parent skill. Confirm compatibility with the installed API schema and CLI help; no source checkout is required.

| Field | What to establish with the user |
|---|---|
| `metadata.name`, `metadata.namespace` | Exact new CR name and namespace; keep realm separate. |
| `spec.realm` | Actual KDC realm. Both creation modes require uppercase ASCII letters, digits, `.`, `_`, `-`; without `.`, `_`, or `-`, require at least three characters. This is a client heuristic, not KDC validation. Do not silently uppercase the user's realm. |
| `spec.daemon.image` | CLI requires `--image`; an explicit image must be nonempty and contain `/` or `:`. A manifest can omit it to inherit the operator's configured image, then its version-based fallback. Confirm the effective image and compatibility. |
| `spec.daemon.krb5Conf` | Full intended `/etc/krb5.conf`: default realm, realm/KDC stanza, DNS discovery choices, and domain mappings or site policy. A supplied nonempty configuration must contain the exact realm and `[realms]` in both creation modes. Omission is permitted only for a confirmed custom image supplying its own config. These checks do not parse Kerberos syntax or test connectivity. |
| `spec.daemon.renewMinutes` | Positive integer when set; effective default **30 minutes**. Choose with ticket lifetime, renewal policy, and KDC load in mind. |
| `spec.daemon.pruneStale` | CLI defaults **true**; omitted manifest field is false. Explicitly choose. It removes caches previously managed by this daemon when UIDs leave the roster; disabling it can leave credentials after disable/delete and force Principal purge timeouts. It does not delete every foreign cache. |
| `spec.daemon.hostCachePath` | Default `/tmp`; host directory mounted at the same path, with `krb5cc_<uid>` files for consumers. Confirm consumer expectations and overlapping daemons/UIDs across Tenants sharing nodes and paths; namespaces do not isolate host files. Check the readiness/cache-path limitation in the parent skill before choosing a non-default path. |
| `spec.daemon.nodeSelector`, `tolerations` | Intended daemon node scope and relevant taints. Empty selector does not mean only worker nodes; actual eligibility depends on scheduling constraints. Verify expected nodes, not just a count. Distinct from Principal `spec.nodeSelector`, which filters credential placement within the Tenant's daemon nodes. |
| `spec.daemon.hostAliases` | Optional `[{ip: ..., hostnames: [...]}]` for pod hostname overrides when DNS is insufficient; use actual mappings. |
| `spec.daemon.imagePullPolicy` | `Always`, `IfNotPresent`, or `Never`; default `IfNotPresent`. |
| `spec.daemon.resources` | Default requests `50m` CPU/`64Mi` memory; limits `200m`/`256Mi` only when neither requests nor limits is supplied. Ask about expected roster size and site requirements; partial settings do not get the full default set. |
| `spec.deletionTimeout` | Principal purge-finalizer timeout, default 60s for nil/zero; e.g. `"60s"`. It is not a Tenant creation timeout or guarantee of credential removal. |
| `spec.eventStalenessThreshold` | Per-UID Event validity duration, nil/zero defaults to `max(5m, 2 × effective renewMinutes)`. Older events fall back to pod-readiness evidence; this does not prove mint success. |
| `spec.disabled` | Default false. True empties the roster while daemon pods remain; cache cleanup depends on pruning. Confirm any intentional initially disabled Tenant and report that limitation. |
