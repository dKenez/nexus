# Deploying nexus into edda

This is the checklist for the edda PR that deploys nexus.

- The nexus repo produces the images. Everything below happens in `dKenez/edda` and follows its CLAUDE.md: a branch per task, conventional commits, a server-side dry-run as evidence, and a decisions row.
- Paths are relative to the edda repo root.

## What is being deployed

nexus runs as a single pod in the `apps` namespace. It combines an admin API, a Discord bot (an outbound gateway connection) and a reconciler loop. It provisions **external** Hetzner VMs for game servers, so no game workload runs in k3s. That keeps the 2026-09-05 decision ("game hosting → external provider, not into k3s") intact, while putting the controller in k3s.

| Need | edda mechanism |
|---|---|
| Image | `ghcr.io/dkenez/nexus:vX.Y.Z`, pinned. It's the first self-built image in edda: make the GHCR package public, or add a SOPS'd `dockerconfigjson` pull secret (a new pattern). |
| Database | A `nexus` role and database on **apps-pg**, via an SGScript. nexus runs its own Alembic migrations at startup. |
| World backups | The tank-static PV `/mnt/apps-shared/nexus` (uid **2017**), which gets sanoid snapshots. |
| Secrets | `config.sops.yaml` (the Argo `sops` plugin) |
| Admin API | An IngressRoute on `nexus.kristof.world`, tailnet only. It needs no public exposure. |
| Egress | `api.hetzner.cloud` (HTTPS), the Primary IP (SSH 22/tcp, A2S query ports over UDP), and Discord's gateway and API. |

## Checklist

1. **`k8s/bootstrap/children/nexus.yaml`**: an Argo Application at wave `"3"`, with `path: k8s/apps/nexus`, `plugin: {name: sops}` and namespace `apps`. Copy the shape of `children/airtrail.yaml`.

2. **`k8s/apps/nexus/`**:
   - Copy the files in `deploy/k8s/` (`deployment.yaml`, `pvc.yaml`, `ingressroute.yaml`) from the nexus repo.
   - Pin `image:` to the released tag.
   - Set `HCLOUD_PRIMARY_IP` to the prod Primary IP's name and `PUBLIC_HOSTNAME` to the Cloudflare name that points at it.

3. **`k8s/apps/nexus/config.sops.yaml`**:
   - Start from `deploy/k8s/secret.example.yaml`. Every key is an environment variable, because the pod uses `envFrom`.
   - Create it with `sops k8s/apps/nexus/config.sops.yaml`.
   - `HCLOUD_TOKEN` is the **production** project's token.
   - Generate two fresh ed25519 keys for `SSH_CLIENT_KEY` and `SSH_HOST_KEY` (`ssh-keygen -t ed25519 -N ''`). Don't reuse the dev keys.

4. **The PV in `k8s/data/pv-apps.yaml`**: add `nexus-backups-pv`, following the `memos-data-pv` shape:
   - `hostPath: {path: /mnt/apps-shared/nexus, type: Directory}`
   - `storageClassName: tank-static`
   - `persistentVolumeReclaimPolicy: Retain`
   - node affinity on `k3s-agent-01`
   - capacity `50Gi` (nominal)

5. **The `apps-shared` subdirectory owner** in `ansible/inventory/host_vars/k3s-agent-01/vars.yml`:
   - Add `{path: nexus, owner: 2017}`, then apply with the guests play `--limit k3s-agent-01`.
   - The directory must exist first because the PV uses `type: Directory` and kubelet applies no fsGroup to hostPath.
   - **Re-check that 2017 is still free** before merging. If it isn't, rebuild the image with `--build-arg UID=<n>` and change `runAsUser` and `runAsGroup` to match.

6. **Database**:
   - Add `k8s/data/apps-pg/sgscript-nexus.yaml`, a copy of `sgscript-airtrail.yaml` without the extension entry. It needs `create-nexus-role` (from the secret) and `CREATE DATABASE nexus OWNER nexus;`.
   - Add `nexus-sql.sops.yaml` holding `01-create-role.sql` with a guarded `CREATE ROLE nexus LOGIN PASSWORD '…'`.
   - Append `- sgScript: apps-pg-nexus` to `managedSql.scripts` in `sgcluster.yaml`.
   - The DSN in `config.sops.yaml` is `postgresql+asyncpg://nexus:<pw>@apps-pg.data.svc.cluster.local:5432/nexus`. Check the service name against another app's DSN.

7. **`docs/plan.md` decisions row**. Record three things:
   - The nexus controller now runs in k3s. Its game VMs stay external (Hetzner) and are created only on demand.
   - World data lives on tank, under nexus's own retention plus sanoid.
   - Custodian's `NEXUS_*` variables still point at the retired nexus VM. They were deliberately left alone, because nexus now has its own bot.

8. **Evidence and rollout**:
   - Run `kubectl apply --dry-run=server -f k8s/apps/nexus/` (the SOPS file decrypted).
   - Sync the child at the branch revision.
   - Check `/readyz`, check that the bot comes online, and run `/games`.
   - The first real `/game start` should be watched in the Hetzner console.

## Operational notes

- **Replicas must stay at 1 with `Recreate`.** nexus serialises VM creation and deletion in-process.
- **Secret changes need `kubectl -n apps rollout restart deploy/nexus`**, because there is no Reloader.
- **Restarts are safe mid-operation.** On startup nexus reconciles with Hetzner and settles interrupted starts and stops. An interrupted stop re-runs its backup before the VM can be deleted.
- **Everything is visible in the admin API.** `GET /api/host` and `GET /api/games` show state; `POST /api/host/shutdown` stops everything cleanly. All `/api` routes need `X-API-KEY`.
- **The Primary IP must have auto-delete off.** nexus refuses to start otherwise, since deleting a VM would otherwise release the IP.
- **Rollback:** scale the Deployment to 0. Any running VM keeps running, but it stops being managed and billing continues. Before scaling down, run `/host shutdown` or `nexus host shutdown`.
