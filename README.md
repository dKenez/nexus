# nexus

nexus runs game servers on Hetzner Cloud on demand, controlled from Discord.

- **`/game start valheim`** creates a Hetzner VM (if none is running), attaches the static Primary IP, restores the latest world backup and starts the server.
- It watches the player count, from the recipe's query: the server log for Valheim, or Steam A2S. When a game has been **empty for `idle_minutes`**, nexus stops it and **pulls the world back to ymir**.
- When the last game stops, the VM **is deleted just before its current paid hour runs out**. Hetzner bills per started hour of a server's life, so keeping the already-paid VM costs nothing, and a restart meanwhile reuses it instantly. After that, nothing is billed while nobody plays.
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
| running | Players are polled every `RECONCILE_INTERVAL` seconds. An empty game past its startup grace and `idle_minutes` is stopped. If the recipe has `[snapshots]`, each new in-game snapshot is copied to `/backups/<game>/snapshots/`. |
| stop | `docker stop` lets the server save. The data directory is streamed to `/backups` (written atomically). Old backups are pruned. If nothing else runs on the VM, it's kept until `HOST_BILLING_MARGIN` seconds (default 300) before its paid hour ends, then deleted. A start before then reuses it. `/host shutdown` deletes it immediately. |

**Safety rules:**
- **A VM is never deleted while any game on it has unsaved data.** If a backup fails, the game is marked `failed` and the VM is kept. Admins are alerted, and a later `/game stop` retries the backup.
- `/host destroy` is the only override.
- **nexus only touches what it owns:**
  - servers labelled `managed-by=nexus` and `nexus-env=<NEXUS_ENV>`
  - the one configured Primary IP
  - the `nexus-host` firewall
- **Startup recovery:** after a restart nexus re-reads Hetzner and the VM's containers, then settles any operation that was interrupted.

## Backups and snapshots

There are two layers, and both only hold what a restore needs:

- **Full backups** (`/backups/<game>/*.tar.zst`) are the game's data directory minus the recipe's `backup_exclude` paths. For Valheim that's the world plus the admin/ban/permit lists and `prefs`. They're taken on every stop and by `/game backup`, and every start restores one: the newest, or the one chosen with `/game restore`. The last `BACKUP_RETENTION` are kept.
- **Snapshots** (`/backups/<game>/snapshots/`) are the game image's own save-aware snapshots, copied off the VM while the game runs. For Valheim that's the hourly `worlds-*.zip` of `worlds_local/`. nexus copies each new one within `check_minutes` and keeps the last `keep`. They cover what full backups can't: losing the VM mid-session, or rolling back to an hour ago.

**Restoring a snapshot** is `/game restore <game>` (or `nexus games restore <game> --snapshot <id>`) with the game stopped. nexus takes the newest full backup, swaps in the snapshot's world, and saves the result as a new full backup. The next start restores that like any other backup.

### Importing a world

`nexus games import <game> <archive>` uploads an archive of a game's data directory and makes it the game's newest backup, so the next start restores it. The game must be stopped.

- **Formats:** `.tar`, `.tar.gz`, `.tar.zst` or `.zip`, made from the directory itself or from its parent. A single wrapping directory such as `config/` is removed automatically.
- **Checks:** the recipe's `[import]` table lists what's required. For Valheim, the world named `WORLD_NAME` must be in `worlds_local/`, otherwise the import is refused before anything changes. `backup_exclude` paths are dropped, the same as in every backup.
- **Safety:** absolute paths and `..` are refused, and links and device files are skipped.

**Moving the current Valheim server to nexus:**
1. On the old host, stop the server so the world is saved, then copy its data out:
   ```sh
   docker stop valheim
   docker cp valheim:/config - > voe-config.tar
   ```
2. Import it into the stopped game:
   ```sh
   uv run nexus games import valheim voe-config.tar
   ```
3. Run `/game start valheim` and check the world.

To try it in dev first without stopping prod, skip `docker stop`. A copy taken while the server runs can catch a save halfway. That's fine for a test, but not for the real move.

## Discord commands

| Command | Tier |
|---|---|
| `/games`, `/game status`, `/host status` | viewer |
| `/game start`, `/game stop` | operator |
| `/game backup`, `/game backups`, `/game restore`, `/host shutdown`, `/host destroy` | admin |

- Tiers map to Discord role IDs (`DISCORD_ROLES_VIEWER`, `DISCORD_ROLES_OPERATOR`, `DISCORD_ROLES_ADMIN`).
- A higher tier includes the lower ones.
- If no viewer roles are set, every guild member can view.
- `/game restore` chooses the full backup or snapshot the *next* start uses. The game must be stopped.

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
4. Join the server, change something in the world, then leave. After the idle period nexus should stop the game, write `.backups/valheim/*.tar.zst`, and delete the VM near the end of its paid hour. The Primary IP should stay and be unassigned.
5. Run `/game start valheim` again. The change should still be there.

## Deployment

- `deploy/k8s/` holds reference manifests in edda's conventions.
- [docs/edda-handoff.md](docs/edda-handoff.md) lists everything edda needs.
