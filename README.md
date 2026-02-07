# muh homelab setup

## "Coma" - Kubernetes cluster

"Coma" is the name of my k8s cluster that handles almost all of the workloads in my homelab.
Named after the Coma Star Cluster, because it felt poetic at the time and kinda ominous.

There used to be another cluster called Pleiades that lived on k3s debian VMs. But it eventually imploded for reasons I still don't fully understand. Rather than resurrect it , I started again. Hence: Coma.

### Cluster Architecutre

#### Hardware

The hardware consists of 3 optiplex mini PCs:

- **coma-master-1** - Core i5 7500, 16GB DDR4 RAM, 256GB NVMe
- **coma-worker-2** - Core i7 6700T, 16GB DDR4 RAM, 500GB NVMe
- **coma-worker-3** - Core i7 6700T 16GB DDR4 RAM, 500GB NVMe

#### Storage

 - **Longhorn** for ISCSI-backed storage on the nodes. Only app SQLlite databases and other app data is stored here hence why I don't use big SSDs on the nodes.
 - **OpenEBS** for local-path storage for Postgres databases.
 - **NFS** mounts from my TrueNAS server to store the big stuff like movies, music, photos, etc.
 - **Cloudflare R2** object storage for database backups.

#### Control Plane

 - Bare metal **Talos**. Control plane and workloads live together because this is a homelab, not AWS.
 - **Flux CD** for gitops, helm, and image update automation.
 - **Lens** IDE for managing the cluster. Don't care that it went closed-source.

#### Networking

- **Cilium** in eBPF mode for CNI and load bal.
- **Envoy Gateway** with Gateway API for reverse proxy.
- **Tailscale** for remote access.

#### Crypto / secrets

- **cert-manager** for TLS certs. Outwards-facing services live under a wildcard domain so no need for external-dns.
- **Vault** for secrets storage, internal CA, and ACME issuer.
- **External Secrets Operator** for syncing vault secrets to k8s secrets.
