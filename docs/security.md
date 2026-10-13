# Security and sanitization

## Publication boundary

Only the build gateway, deployment workflow, and chart patterns were used as design references. New example files were written with generic identifiers. Private history, clients, application/service names, production manifests, internal DNS/IP mappings, credentials, certificates, user records, database exports, logs, VPN configuration, backups, and Terraform state were excluded. The source repository was not modified.

All endpoints use reserved example domains. Namespace names, labels, image paths, Secret names, and Vault paths are synthetic. BuildKit, GitLab, Harbor, Helm, Vault, Kubernetes, Terraform, FluxCD, and Prometheus are public technology names retained to describe the architecture.

The existing PNG was visually reviewed and the SVG/text files were scanned. No private project names or endpoints were found in the template. Pattern scanning cannot prove absence of every possible secret; review any future customization before publication.

## Runtime boundary

- The build API requires a token and fixes its output repository in administrator configuration. The uploader sends only an explicit sample-file allowlist.
- Archive extraction rejects traversal, absolute paths, links, and special files; compressed and expanded sizes and file count are bounded.
- BuildKit is privileged and executes submitted Dockerfiles. Namespace isolation, authentication, and NetworkPolicy do not make it safe for hostile multi-tenant execution. Use trusted teams and dedicated build nodes.
- NetworkPolicy relies on an enforcing CNI. Kubernetes RBAC and GitLab protection settings remain necessary; labels alone do not authenticate callers.
- The ClusterIP API uses plaintext HTTP by default. Use a service mesh with mTLS or an authenticated TLS proxy for networks where traffic could be observed. Do not expose the API publicly.
- Build logs are suppressed rather than copied into HTTP responses or CI output. No arbitrary host context paths, caller-selected repositories, registry login commands, or daemon TCP endpoints are exposed.
- Secrets are referenced rather than embedded. Ignore rules are a backstop; they cannot remove already committed secrets from history.
- The app-deployer Role can read namespace secrets and deploy application code. Keep its credentials away from untrusted branches and users.

## Before publishing a customized fork

Review the complete file set, Git diff, SVG text, image content, and Git history with a secret scanner. Check names, comments, URLs, email addresses, IPs, encoded blobs, and generated artifacts. Keep private configuration outside the public tree. If a real credential is discovered in history, revoke/rotate it before cleaning the history.
