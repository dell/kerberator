# Private registries and building your own images

Kerberator ships two images and one chart per release:

| Artifact | Default location |
|---|---|
| Operator | `ghcr.io/dell/kerberator-operator:<version>` |
| Daemon | `ghcr.io/dell/kerberator-daemon:<version>` |
| Helm chart | `oci://ghcr.io/dell/charts/kerberator` (installed with `--version <x.y.z>`) |

Nothing at runtime depends on the registry after the images are pulled.

## Mirror the published images

```bash
VERSION=v0.10.0
MIRROR=registry.example.com/kerberator

crane copy ghcr.io/dell/kerberator-operator:$VERSION $MIRROR/kerberator-operator:$VERSION
crane copy ghcr.io/dell/kerberator-daemon:$VERSION   $MIRROR/kerberator-daemon:$VERSION
```

`skopeo copy docker://... docker://...` works the same way. Then point the
chart at the mirror:

```bash
helm install kerberator oci://ghcr.io/dell/charts/kerberator --version 0.10.0 \
  --namespace kerberator-system --create-namespace \
  --set image.repository=$MIRROR/kerberator-operator \
  --set daemon.image=$MIRROR/kerberator-daemon:$VERSION \
  --set 'imagePullSecrets[0].name=my-pull-secret'
```

`daemon.image` is the image the operator gives every Tenant's DaemonSet. A
`Tenant` can override it with `spec.daemon.image`, but with the chart value
set you should not need to.

The daemon DaemonSet does not inherit the chart's `imagePullSecrets`. It
runs as the `default` ServiceAccount in each Tenant namespace. Either use a
mirror that allows anonymous pulls, or attach the pull secret to that
ServiceAccount in each Tenant namespace:

```bash
kubectl -n kerberator-demo patch serviceaccount default -p '{"imagePullSecrets":[{"name":"my-pull-secret"}]}'
```

If the chart itself must come from your mirror, `helm pull` it once and push
it with `helm push` to your OCI registry.

## Build from source

```bash
make images push IMAGE_PREFIX=registry.example.com/kerberator VERSION=dev
```

`make images` uses `docker` by default. Set `CONTAINER_TOOL=buildah` (or
`podman`) to use something else. Then install the chart from the working tree
with the same two `--set` flags as above, pointing at the tag you built.

## Air-gapped clusters

Mirror the two images and the cert-manager images your cluster needs, push
the chart to an internal OCI registry, and install as above. The daemon needs
network access to your KDC from every node, nothing else.
