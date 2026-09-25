# nexus

nexus runs game servers on Hetzner Cloud on demand, controlled from Discord.

- **`/game start valheim`** creates a Hetzner VM (if none is running), attaches the static Primary IP, restores the latest world backup and starts the server.
- It watches the player count (Steam A2S). When a game has been **empty for `idle_minutes`**, nexus stops it and **pulls the world back to ymir**.
- When the last game stops, it **deletes the VM**, so nothing is billed while nobody plays.
- Several games can share the one VM, each as its own container on its own ports.

nexus itself runs in edda's k3s cluster. The game servers never do.

## How it works

```text
Discord ──▶ nexus (k3s pod) ──hcloud API──▶ Hetzner: VM + Primary IP + firewall
               │   │                               │
               │   └── SSH (tar|zstd streams) ◀────┘ docker containers per game
               │
               ├── Postgres (apps-pg): hosts, game state, backup index, audit log
               └── /backups (tank): <game>/<timestamp>-<reason>.tar.zst
```

**Lifecycle of a game:**

| Step | What happens |
|---|---|
| start | Capacity is checked (the sum of each game's `memory_mb` against the VM's memory). A VM is provisioned if none exists and the firewall is opened. The world is restored and the container is run. |
| running | Players are polled every `RECONCILE_INTERVAL` seconds. An empty game past its startup grace and `idle_minutes` is stopped. |
| stop | `docker stop` lets the server save. The data directory is streamed to `/backups` (written atomically). Old backups are pruned, and the VM is deleted if nothing else runs on it. |

**Safety rules:**
- **A VM is never deleted while any game on it has unsaved data.** If a backup fails, the game is marked `failed` and the VM is kept. Admins are alerted, and a later `/game stop` retries the backup.
- `/host destroy` is the only override.
- **nexus only touches what it owns:**
  - servers labelled `managed-by=nexus` and `nexus-env=<NEXUS_ENV>`
  - the one configured Primary IP
  - the `nexus-host` firewall
- **Startup recovery:** after a restart nexus re-reads Hetzner and the VM's containers, then settles any operation that was interrupted.

## Discord commands

| Command | Tier |
|---|---|
| `/games`, `/game status`, `/host status` | viewer |
| `/game start`, `/game stop` | operator |
| `/game backup`, `/game backups`, `/game restore`, `/host shutdown`, `/host destroy` | admin |

- Tiers map to Discord role IDs (`DISCORD_ROLES_VIEWER`, `DISCORD_ROLES_OPERATOR`, `DISCORD_ROLES_ADMIN`).
- A higher tier includes the lower ones.
- If no viewer roles are set, every guild member can view.
- `/game restore` pins a backup for the *next* start. The game must be stopped.

## Recipes

A recipe is `recipes/<name>/recipe.toml`: which image to run, its ports, what to back up and how to count players. See [recipes/valheim/recipe.toml](recipes/valheim/recipe.toml), which runs [`lloesche/valheim-server`](https://github.com/lloesche/valheim-server-docker), the same image the current server uses.

- The image must keep everything it persists under `data_path`, which is backed up whole.
- The image must also create its own defaults when `data_path` is empty, since that's what a brand-new world looks like.
- Secrets listed in `secret_env` are read from `NEXUS_GAME_<GAME>_<KEY>`.
- Fresh VMs pull images anonymously. For a private GHCR image, set `GHCR_PULL_USER` and `GHCR_PULL_TOKEN`.

## Development

Tooling is pinned in `mise.toml`: Python, uv, prek and the hcloud CLI.

```sh
mise run setup        # uv sync + prek hooks (pre-commit and commit-msg)
cp .env.example .env  # fill in the DEV Hetzner project, test Discord bot, SSH keys
mise run check        # ruff, ty, pytest
mise run dev          # compose Postgres + nexus on :8080
```

Other tasks: `mise tasks`. The CLI talks to a running nexus: `NEXUS_API_TOKEN=... uv run nexus games list`.

Commits follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(bot): …`, `fix(orchestrator): …`). The commit-msg hook enforces this.

### Hetzner dev safety

- **Never use the production Hetzner token locally.** Development and smoke tests run in a separate **dev project** with its own Primary IP.
- With `NEXUS_ENV=dev`, nexus refuses to start if the token can see `HCLOUD_FORBIDDEN_PRIMARY_IP`, the prod IP's name.
- Run `mise run hcloud:ls` to check which project the hcloud CLI is pointed at.
- The unit tests never contact Hetzner.
- `NEXUS_TEST_DATABASE_URL` runs the test suite against a throwaway Postgres.

### Manual smoke test (dev project)

1. Run `mise run hcloud:ls` and confirm the dev project is active.
2. Start nexus: `mise run dev`, with `idle_minutes = 2` in a local recipe copy if you want a quick idle test.
3. Run `/game start valheim`. Expect a VM holding the dev Primary IP and a "ready" message.
4. Join the server, change something in the world, then leave. After the idle period nexus should stop the game, write `.backups/valheim/*.tar.zst` and delete the VM. The Primary IP should stay and be unassigned.
5. Run `/game start valheim` again. The change should still be there.

## Deployment

- `deploy/k8s/` holds reference manifests in edda's conventions.
- [docs/edda-handoff.md](docs/edda-handoff.md) lists everything edda needs.
