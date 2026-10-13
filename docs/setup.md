# Setup

## Prerequisites

Use a test Kubernetes cluster with a NetworkPolicy-enforcing CNI and dedicated Linux build nodes, a TLS-enabled Harbor-compatible registry, Helm 3, kubectl with Kustomize, Python 3.12 or newer, and administrator-managed GitLab Kubernetes runners. Optional Vault synchronization requires External Secrets Operator with `external-secrets.io/v1` CRDs. Optional CPU HPA requires metrics-server.

Examples use `registry.example.com` and `vault.example.com`; replace them with your own configuration. Keep private customization in a private fork or ignored local files. Pin image digests and review dependency versions before production use. BuildKit v0.25.0 is an explicit example version, not a claim that it is the newest release.

## 1. Build administrator-owned images

Authenticate to your registry using your approved credential manager. Build and push the generic gateway and CI tools images from their small, allowlisted contexts:

```sh
docker build -t registry.example.com/platform/build-service:template build-service
docker build -t registry.example.com/platform/ci-tools:template ci
docker push registry.example.com/platform/build-service:template
docker push registry.example.com/platform/ci-tools:template
```

Update the gateway image in `k8s/build/base/deployment.yaml` and `CI_TOOLS_IMAGE` in `.gitlab-ci.yml`. Update the registry host in CI, all build overlays, and Helm environment values consistently. The sample uploader verifies that the API returns the same image repository the pipeline expects.

## 2. Provision namespaces and access

Review `k8s/admin/namespaces-rbac.yaml`, then apply it as an administrator:

```sh
kubectl apply -f k8s/admin/namespaces-rbac.yaml
```

Each environment uses `build-dev`, `build-test`, or `build-prod` for the privileged build workload and `app-dev`, `app-test`, or `app-prod` for CI jobs and application deployment. Place build pods on dedicated nodes by adding your own nodeSelector/tolerations. The build namespace allows privileged pods; application namespaces enforce restricted Pod Security. Do not grant developers deployment, pod creation, exec, or Secret permissions in build namespaces.

The `app-deployer` service account has permissions needed by this chart and Helm's Secret-backed release storage. These include access to namespace secrets; this identity must be treated as privileged within its application namespace. It has no cross-namespace RoleBinding. Configure the GitLab runner manager separately; its pod-management permissions are not supplied by the app-deployer Role.

## 3. Provision secrets outside Git

Required existing Kubernetes Secrets:

| Namespace | Secret | Type / key | Purpose |
| --- | --- | --- | --- |
| Each build namespace | `build-api` | Opaque / `token` | Unique random token of at least 32 characters |
| Each build namespace | `registry-push` | kubernetes.io/dockerconfigjson / `.dockerconfigjson` | Push credentials limited to that environment's sample image project |
| Each build namespace | `registry-pull` | kubernetes.io/dockerconfigjson / `.dockerconfigjson` | Pull the administrator gateway image |
| Each app namespace | `registry-pull` | kubernetes.io/dockerconfigjson / `.dockerconfigjson` | Pull the sample application and CI tools images |

Supply these through your secret manager or local protected files. Do not paste credential values into manifests, commands saved in shell history, chart values, or CI YAML. The build API reads the registry configuration from `/registry-auth/config.json`; credentials are never passed to `buildctl` in command-line arguments. Use separate pull and push identities.

The optional `k8s/optional/vault-secrets.yaml` demonstrates Kubernetes authentication to Vault and synchronization of the DEV API token and registry push configuration. Configure a Vault Kubernetes auth role bound to `vault-sync` in `build-dev`, a read-only KV v2 policy for `delivery/dev/*`, and trusted TLS certificates. Store `token` under `delivery/dev/build-api`, and a complete Docker config JSON string in `dockerconfigjson` under `delivery/dev/registry-push`. Recreate the example independently for TEST and PROD with distinct roles and paths. Pull Secrets are provisioned separately. API token changes require a gateway rollout because the token is read at startup; update the matching GitLab variable as part of rotation.

## 4. Deploy the build gateways

Apply reviewed environment overlays as an administrator:

```sh
kubectl apply -k k8s/build/overlays/dev
kubectl apply -k k8s/build/overlays/test
kubectl apply -k k8s/build/overlays/prod
kubectl rollout status deployment/build-service -n build-dev
```

The API runs as UID/GID 1000. BuildKit owns the pod-local socket and grants group 1000 access. Only the BuildKit container is privileged. Context uploads live on a bounded API temporary volume; BuildKit receives context through the client session. Cache uses ephemeral storage and is lost on pod replacement.

The default policy allows the matching app namespace's pods labeled `delivery-role: ci-build` and the `monitoring` namespace to reach port 8080. DNS egress assumes cluster DNS in `kube-system`; adapt it for NodeLocal DNS. HTTPS egress allows any destination on port 443 so public base images/package mirrors can work; narrow it to your approved endpoints with your CNI's supported controls. Registry/package services on other ports require explicit policy changes.

## 5. Configure GitLab

Provision three administrator-controlled Kubernetes runners with tags `delivery-dev`, `delivery-test`, and `delivery-prod`. Use the corresponding app namespace and configure the job pod label `delivery-role: ci-build`. Disable privileged mode, host mounts, and developer overrides of namespaces, service accounts, and pod labels. Set runner job/helper security contexts compatible with restricted Pod Security, and configure the runner's image pull Secret for the private tools image. Register runners out of band; no registration token belongs in this repository.

Configure GitLab variables separately scoped to `dev`, `test`, and `prod`:

| Variable | Configuration |
| --- | --- |
| `BUILD_API_TOKEN` | Masked/hidden secret matching the build namespace; protect PROD |
| `KUBECONFIG` | File variable with a restricted app-deployer identity; protect PROD |

Set protected PROD environments, protected runners, and deployment approvals according to your GitLab edition. An environment selection dropdown is not an authorization boundary. Anyone allowed to edit CI scripts and access a credential can use that credential; restrict protected branches and runner access accordingly. Prefer short-lived deployment credentials or your existing GitLab Agent setup; never put a cluster-admin kubeconfig in CI.

Use GitLab's CI Lint before the first run. Start a pipeline through **Run pipeline**, select `TARGET_ENV`, and use the manual deploy action after validation and image build succeed. There is no automatic deployment on push. The commit SHA tag avoids `latest`, but registry tag immutability must be enabled separately.

## 6. Verify the application

After successful DEV deployment:

```sh
kubectl rollout status deployment/sample-app -n app-dev
kubectl port-forward service/sample-app -n app-dev 8080:80
```

Visit `http://localhost:8080/health` and check the returned environment. No public ingress or production DNS is created by this chart. Repeat environment isolation and credential permission checks before enabling TEST or PROD.

## Optional scaling and monitoring

Scrape `/metrics` on the gateway using your existing Prometheus setup; scrape annotations require matching Prometheus configuration. `k8s/optional/hpa.yaml` is an illustrative DEV CPU-based HPA, not slot-based scaling. Slot metrics require a custom metrics adapter and matching mapping, which are not supplied here. Every replica allows two concurrent builds; aggregate capacity grows with replica count.

SIGTERM stops new gateway work and allows bounded requests to drain. Kubernetes may terminate BuildKit concurrently, so pod rollout/scale-down can still interrupt builds. Schedule changes when idle and retry failed pipelines explicitly. The HPA stabilization window reduces churn but does not provide durable jobs or interruption-free scale-down.

## References

Checked on 2026-10-13: [BuildKit usage](https://github.com/moby/buildkit/blob/master/README.md), [GitLab CI YAML](https://docs.gitlab.com/ci/yaml/), [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/), and [External Secrets Vault provider](https://external-secrets.io/latest/provider/hashicorp-vault/).
