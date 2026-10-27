# PoP Monitor on the server: automatic updates

```
push to main ──► GitHub Actions: tests, builds the images whose code changed,
                 publishes them (ghcr.io/postfach/pop-*), then commits their exact
                 versions to the `deploy` branch
                        │
                        ▼
Portainer stack follows the `deploy` branch, checks every 5 minutes,
and redeploys: only the containers whose image changed restart
```

A failed test stops the run before anything is published, so the server keeps running
what it has. Each image is tagged with a hash of its own code (`src-…`), so a change to
the Bookings page restarts the receiver only; the gateway keeps watching the cameras.

## Setting it up (once)

### 1. GitHub

* **Settings › Actions › General › Workflow permissions**: *Read and write permissions*
  (the workflow pushes the `deploy` branch and publishes the images).
* Push to `main` (or **Actions › release › Run workflow**). The first run builds all
  three images (10–15 minutes) and creates the `deploy` branch.
* The images are private on GitHub (even while the repository is public), so the server
  needs a login to fetch them: **your profile › Settings › Developer settings › Personal
  access tokens › Tokens (classic) › Generate new token**, scope `read:packages` (add
  `repo` too if the repository will be private), a long expiry (note it in the calendar).

### 2. Portainer (on the server)

1. **Take a backup** first: Health › Download a backup.
2. Note the current stack's volume names in **Volumes** (e.g. `pop-monitor_pop-received`):
   the part before `_pop-` is the prefix the new stack must use to keep the data.
3. Put the files the stack reads in `/opt/pop-monitor` on the server (or another folder:
   set `POP_HOME`):
   * `config.json` (the gateway's settings, the same file as now),
   * `bookings/` and `recordings/` (can be empty).
4. **Registries › Add registry › Custom registry**: name `GitHub`, URL `ghcr.io`,
   authentication on, your GitHub user name and the token from step 1.
5. Stop the old stack: **Stacks › (old stack) › Stop** (don't remove its volumes).
6. **Stacks › Add stack › Repository**:
   * Name: `pop-monitor`
   * Repository URL: `https://github.com/postfach/pop-monitor`
   * Repository reference: `refs/heads/deploy`
   * Compose path: `compose.yml`
   * Authentication: only once the repository is private (your user name and the token).
   * **GitOps updates** on, mechanism **Polling**, interval `5m`. (*Re-pull image* and
     *Force redeployment* aren't needed: every new version is a new commit.)
     On a stack that already exists, this is under **Stacks › pop-monitor › Edit stack
     settings** (or the stack's **Editor** tab in some Portainer versions). Without it the
     stack only updates when you click **Pull and redeploy**.
   * **Environment variables**: *Load variables from .env file* with your current
     `.env`, plus `POP_VOLUME_PREFIX` if step 2 showed another prefix than `pop-monitor`,
     and `POP_HOME` if not `/opt/pop-monitor`. All the variables are in
     `deploy/server/stack.env.example`.
7. **Deploy the stack**. Check the Health page: same gateway, sites and history. Then the
   old stack can be removed (again keeping the volumes).

## Day to day

* Changes go to `main` (Claude commits and pushes them when they are tested). The server
  is on them about 10–15 minutes later: watch **Actions** on GitHub for the build, and the
  stack's **Git** info in Portainer for the deploy.
* Change a setting or secret: **Stacks › pop-monitor › Environment variables › Update the
  stack**. Change `config.json`: edit it in `/opt/pop-monitor`, then restart the gateway.
* Go back to an earlier version: on GitHub, revert the latest commit on the `deploy`
  branch (or the change on `main`); the server follows within 5 minutes.
* Rebuild everything (e.g. for base-image security updates): **Actions › release › Run
  workflow** with *Build every image* ticked.
