# Deploying nexus into edda

This is the checklist for the edda PR that deploys nexus, and the runbook for moving the current Valheim server onto it.

- The nexus repo builds the image. The edda part happens in `dKenez/edda` and follows its CLAUDE.md: a branch per task, conventional commits, a server-side dry-run as evidence, and a decisions row.
- Paths are relative to the edda repo root unless they start with `deploy/` or `recipes/`, which are in this repo.

## What is being deployed

nexus runs as a single pod in the `apps` namespace. It contains an admin API, a Discord bot (an outbound gateway connection, so no public ingress) and a reconciler loop. It creates **external** Hetzner VMs for game servers only while someone plays, so no game workload runs in k3s. That keeps the 2026-09-05 decision ("game hosting → external provider, not into k3s") intact, while putting the controller in k3s.

| Need | edda mechanism |
|---|---|
| Image | `ghcr.io/dkenez/nexus:vX.Y.Z`, pinned. It's the first self-built image in edda: make the GHCR package public, or add a SOPS'd `dockerconfigjson` pull secret (a new pattern). |
| Database | A `nexus` role and database on **apps-pg** (`apps-pg.data.svc.cluster.local`), via an SGScript. nexus runs its own Alembic migrations at startup. |
| World backups | The tank-static PV `/mnt/apps-shared/nexus`, owned by uid **2017** (still free as of 2026-09-26), which gets sanoid snapshots. |
| Secrets | `config.sops.yaml` through the Argo `sops` plugin. The pod uses `envFrom`, so every key is an environment variable. |
| Admin API | An IngressRoute on `nexus.kristof.world`, tailnet only. |
| Egress | `api.hetzner.cloud` over HTTPS, and SSH (22/tcp) to the Primary IP. SSH carries backups, restores and the game-log player count. Plus Discord's gateway and API. |

## Prerequisites outside edda

**Hetzner (production project):**
- The Primary IP your Cloudflare name points at must have **auto-delete off**. nexus refuses to start otherwise, because deleting a VM would release the IP.
- At cutover the IP must be **unassigned**, because nexus attaches it when it creates a VM. See the cutover steps.
- Create an API token with read and write access.
- Choose `HCLOUD_SERVER_TYPE`. Dev used `cpx32` (8 GB, about €0.071/h), which fits Valheim (4 GB) plus another small game. The manifest says `cx32`; set whichever you want.

**Discord:**
- Use a production bot application, separate from the dev bot, so dev can never touch prod.
- Invite it with `scope=bot+applications.commands&permissions=19456` (View Channels, Send Messages, Embed Links).
- The bot needs **View Channel, Send Messages and Embed Links in the notify channel itself**. Private channels have overrides that block this. The bot checks at startup and logs the fix if it's missing.
- Collect the role IDs for the three tiers. The admin tier is for backups, restores and `/host`.

**GHCR:** after the first release, make `ghcr.io/dkenez/nexus` public, or use a pull secret.

## Checklist (edda PR)

1. **`k8s/bootstrap/children/nexus.yaml`**: an Argo Application at wave `"3"`, with `path: k8s/apps/nexus`, `plugin: {name: sops}` and namespace `apps`. Copy the shape of `children/airtrail.yaml`.

2. **`k8s/apps/nexus/`**: copy `deploy/k8s/deployment.yaml`, `pvc.yaml` and `ingressroute.yaml`, then set:
   - `image:` to the released tag;
   - `HCLOUD_PRIMARY_IP` to the prod Primary IP's name, ID or address;
   - `PUBLIC_HOSTNAME` to the Cloudflare name that points at it (it's shown to players);
   - `HCLOUD_SERVER_TYPE`;
   - `TIMEZONE`, which is already `Europe/Copenhagen`.

   Optional tuning (defaults in brackets): `HOST_BILLING_MARGIN` (300) is how many seconds before the paid hour ends an empty VM is deleted. `PLAYERS_ALERT_HOURS` (6) is when to warn about a player count that never drops. `BACKUP_RETENTION` (10) is how many full backups to keep per game.

3. **`k8s/apps/nexus/config.sops.yaml`**: start from `deploy/k8s/secret.example.yaml` and create it with `sops k8s/apps/nexus/config.sops.yaml`.
   - `HCLOUD_TOKEN` is the **production** project's token.
   - `SSH_CLIENT_KEY` and `SSH_HOST_KEY` are two **new** ed25519 keys (`ssh-keygen -t ed25519 -N ''`). Don't reuse the dev keys.
   - `NEXUS_GAME_VALHEIM_SERVER_PASS` is the Valheim password, at least 5 characters. The variable is named after the image's own setting, `SERVER_PASS`.
   - The `DISCORD_*` values are the prod bot's token, guild, notify channel and role IDs.
   - `DATABASE_URL` is `postgresql+asyncpg://nexus:<pw>@apps-pg.data.svc.cluster.local:5432/nexus`.

   Don't set `HCLOUD_FORBIDDEN_PRIMARY_IP` in prod. It's a dev-only guard against using the prod token by mistake.

4. **The PV in `k8s/data/pv-apps.yaml`**: add `nexus-backups-pv`, following the `memos-data-pv` shape:
   - `hostPath: {path: /mnt/apps-shared/nexus, type: Directory}`
   - `storageClassName: tank-static`
   - `persistentVolumeReclaimPolicy: Retain`
   - node affinity on `k3s-agent-01`
   - capacity `50Gi` (nominal; the world is about 20 MB per compressed backup)

5. **The `apps-shared` subdirectory owner** in `ansible/inventory/host_vars/k3s-agent-01/vars.yml`:
   - Add `{path: nexus, owner: 2017}`, then apply with the guests play `--limit k3s-agent-01`.
   - The directory must exist first, because the PV uses `type: Directory` and kubelet applies no fsGroup to hostPath.
   - If 2017 has been taken by then, rebuild the image with `--build-arg UID=<n>` and change `runAsUser` and `runAsGroup` to match.

6. **Database**:
   - Add `k8s/data/apps-pg/sgscript-nexus.yaml`, a copy of `sgscript-airtrail.yaml` without the extension entry. It needs `create-nexus-role` (from the secret) and `CREATE DATABASE nexus OWNER nexus;`.
   - Add `nexus-sql.sops.yaml` holding `01-create-role.sql` with a guarded `CREATE ROLE nexus LOGIN PASSWORD '…'`.
   - Append `- sgScript: apps-pg-nexus` to `managedSql.scripts` in `sgcluster.yaml`.

7. **`docs/plan.md` decisions row**. Record:
   - The nexus controller runs in k3s. Its game VMs stay external (Hetzner), created on demand and deleted when their paid hour runs out.
   - World data lives on tank, under nexus's own retention plus sanoid.
   - Custodian's `NEXUS_*` variables still point at the retired nexus VM. They were left alone on purpose, because nexus now has its own bot.

8. **Evidence and rollout**:
   - Run `kubectl apply --dry-run=server -f k8s/apps/nexus/` with the SOPS file decrypted.
   - Sync the child at the branch revision.
   - Check `/readyz`, and check the log shows `notifications go to #<channel>` and `synced N commands`.
   - Run `/games` in Discord. It should show Valheim as stopped, because no world has been imported yet.

## Cutover: moving the current Valheim server to nexus

Do this once the pod runs. Players see no server between steps 1 and 5, which takes about 10 minutes.

1. **Stop the old server so it saves**, on the current prod VM: `docker stop valheim`.
2. **Export the world** without the image's old zips:
   ```sh
   docker run --rm --volumes-from valheim alpine tar -C /config --exclude=./backups -cf - . > voe-config.tar
   ```
   Check it's roughly 175 MB and contains `worlds_local/VoE/`: `tar -tf voe-config.tar | head`.
3. **Free the Primary IP.** nexus can only attach it to a VM it creates, and the old VM holds it. nexus deliberately won't touch VMs it didn't create.
   - Power the old VM off and unassign the IP in the Hetzner console. Keep the VM, powered off, until the new server is confirmed. Hetzner still bills a powered-off server, so delete it afterwards.
   - Or, if nothing else runs on the old VM, delete it. That releases the IP too.

   Either way, check the IP shows **no assignee** and auto-delete **off**.
4. **Import the world** into nexus through the admin API, from any tailnet machine:
   ```sh
   NEXUS_URL=https://nexus.kristof.world NEXUS_API_TOKEN=… uv run nexus games import valheim voe-config.tar
   ```
   Expect about 98 files, and `worlds_local/*_backup_auto-*` listed as excluded if old Valheim auto-backups were present.
5. **Start it** with `/game start valheim`. It takes about 2–3 minutes, including creating the VM. Join at the Cloudflare name, port 2456, and check the world.
6. **Clean up:** delete the old VM, if you kept it, once the world checks out.

**Rolling back the cutover:** stop the game in nexus (`/game stop valheim`, which backs up), scale the nexus Deployment to 0, reassign the IP to the old VM and power it on. The old container still has the world as it was at step 1.

## Operational notes

- **Replicas must stay at 1 with `Recreate`.** nexus serialises VM creation and deletion in-process.
- **Secret changes need `kubectl -n apps rollout restart deploy/nexus`**, because there's no Reloader.
- **Restarts are safe mid-operation.** On startup nexus reconciles with Hetzner, settles interrupted starts and stops, and announces a recovered server. An interrupted stop re-runs its backup before the VM can be deleted.
- **Billing:** an empty VM is kept until its paid hour is nearly over, then deleted. A start in that window reuses it. The notify channel shows each VM's lifetime and cost.
- **Everything is visible in the admin API.** `GET /api/host` and `GET /api/games` show state, including `start_seconds` and `delete_at`. `POST /api/host/shutdown` stops everything cleanly. All `/api` routes need `X-API-KEY`.
- **Rollback of nexus itself:** run `/host shutdown` first, then scale the Deployment to 0. A VM left running is no longer managed, and billing continues.
