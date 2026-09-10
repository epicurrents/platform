# Testing the bootstrap scripts on real hosts

The mocked dry-run tests in [scripts/tests/](../../scripts/tests/) exercise the bootstrap scripts' control flow against stub binaries. They catch typos, wrong flags, reordered steps and missed distro branches, and they run in seconds. What they cannot catch is what a real container runtime does with the compose graph — which is the tier that has historically broken fresh deployments, and the gap [ROADMAP.md](../../ROADMAP.md) records as the outstanding Tier 3 work.

This is how to close that gap for one change: two throwaway cloud instances, one per distro family, torn down when the run is over. It costs about five cents.

An ephemeral instance is the right shape for this rather than a local VM. The scripts install a container engine system-wide, enable a systemd socket, and relabel a bind mount under SELinux — none of which is faithfully reproducible in WSL, and all of which wants a machine you are willing to destroy rather than one you have to clean up.

## Shape of a run

| | Docker path | Podman path |
|---|---|---|
| Image | `ubuntu-24.04` | `rocky-9` (or `alma-9`) |
| Script under test | [scripts/bootstrap.sh](../../scripts/bootstrap.sh) | [scripts/bootstrap-podman.sh](../../scripts/bootstrap-podman.sh) |
| Type | `cx33` (4 vCPU, 8 GB) | same |
| Location | `hel1` | same |

`cx33` rather than the cheaper `cx23`: 8 GB is what the frontend build wants, and a 4 GB instance needs swap added before bootstrapping or the build is OOM-killed. Helsinki keeps the EEA rule the rest of this directory follows, though a throwaway instance holds no personal data.

Hetzner has no RHEL image. Rocky and Alma are bit-compatible rebuilds and are what [scripts/bootstrap-podman.sh](../../scripts/bootstrap-podman.sh) accepts by `ID`; genuine RHEL needs a Developer Subscription and a custom image, which is a different exercise. `cpx11` / `cpx21` / `cpx31` are US-only — the EU equivalents are `cpx12` / `cpx22` / `cpx32`, and asking for a US type in an EU location fails with `unsupported location for server type`.

## Before you start

**Check the project's server limit.** It is 4 here, and three are permanent, so the two test instances cannot coexist — run Docker first, destroy it, then run Podman. Creating the second while the first is deleting fails with `Primary IP limit exceeded`, because the address outlives the server by a few seconds.

**Give the test instances their own firewall**, SSH-only, from your own address:

```bash
# One inbound rule: tcp/22 from $(curl -s https://api.ipify.org)/32. Nothing else.
```

Do not attach `web-firewall` — it is the application hosts' and is attached to two live servers. And note the address drifts: a domestic connection can change IP between one session and the next, so re-read it rather than reusing yesterday's.

## Preparing an instance

Hetzner images give you `root` and nothing else, and both bootstraps refuse to run as root. Create the deployment account first — the uid is what matters, not the name, because every service runs as 1000:1000 against a bind mount:

```bash
# Debian/Ubuntu
adduser --disabled-password --gecos "" --uid 1000 epicurrents
# RHEL family
useradd --uid 1000 --create-home epicurrents
```

Then give it sudo (`NOPASSWD` — the Podman path sudoes every compose call, and a password prompt mid-build stalls the run), copy `root`'s `authorized_keys` across, and install `git`.

**On the RHEL side, set SELinux to Enforcing before testing.** Hetzner's Rocky image ships **Permissive**, which silently passes the exact failure a real RHEL host produces. `setenforce 1` plus the matching edit to `/etc/selinux/config` takes a second and is the difference between testing the thing and testing a lookalike.

## Getting the code onto the instance

The bootstraps need a git checkout — they initialise submodules — so clone the public repository and overlay whatever is not pushed yet:

```bash
git clone https://github.com/epicurrents/platform.git ~/platform
# then, from the workstation, scp the changed scripts over and compare checksums
```

Compare `md5sum` on both sides afterwards. It takes a moment and it removes the possibility of spending an hour testing the wrong code.

## Two things the run will stop on

**The frontend build does not work from a fresh clone.** `frontend/viewer` is a shell: its workspace packages and its lockfile are fetched by the viewer's own `npm run setup`, which neither bootstrap nor the `frontend-build` compose service ever runs. The step therefore dies on `npm ci`, and — because `set -e` ignores a failure in a non-final member of an `&&` list — the service carries on and reports `Vite not found` instead, which points at the wrong thing. Until that is fixed, neutralise the one service and leave every other step real:

```yaml
# docker-compose.override.yml, on the instance only
services:
  frontend-build:
    command: ["sh", "-ec", "echo HARNESS-STUB: frontend build skipped"]
```

The override reaches only steps invoked through bare `$COMPOSE`; `$COMPOSE_PROD` passes explicit `-f` flags, so the stack, the vendoring and the lead fields all stay real. Pre-create `frontend/dist/index.html` and `frontend/viewer-dist/` so the web container has something to serve.

**The first pass stops for a `.env` review, and two values must be filled or the second pass aborts.** `FRONTEND_URL` is guarded against its placeholder and refuses to start the app; `ALLOWED_HOSTS` needs the instance address. Note that `PROXY_DOMAIN` ships *commented out*, so testing the TLS overlay means appending the line, not editing it — a `sed s/^PROXY_DOMAIN=.*/` matches nothing and quietly tests the wrong branch.

**Never pipe a first pass into a log.** `init_env` prints every generated secret once, and the script says so. Filter the stream (`grep -avE 'PASSWORD|SECRET|KEY=|PASSPHRASE'`) or accept that the log has to be shredded afterwards.

## What to verify

A green exit code is not the assertion. Check that each step left its artefact behind:

| Step | Evidence |
|---|---|
| Vendored runtime | `frontend/vendor/pyodide/` populated |
| Lead fields | `frontend/vendor/leadfields/manifest.json` plus a `.bin` |
| Project activation | `manage.py showmigrations <project>` shows `[X]` |
| Stack | every service `Up`, and healthy where a healthcheck exists |
| TLS overlay | `caddy` present in `ps` with 80/443 bound, after setting `PROXY_DOMAIN` |

On the application itself, expect **301, not 200**. `DJANGO_MODE=production` redirects to HTTPS and a bare instance has no TLS terminator, so a 301 to `https://…` *is* the app answering. Confirm with the container healthcheck or gunicorn's `Booting worker` lines rather than chasing the redirect.

## Tearing down

Delete the servers, wait for them to actually go, then the firewall — it refuses with `resource_in_use` while a server still references it, and the reference outlives the delete call by a few seconds. Retry rather than assuming the first 422 means something is wrong. Confirm the project is back to its permanent servers and firewalls before walking away; an instance left running is the only way this exercise costs real money.

Finally, if an instance reused an address you have connected to before, drop the stale host key (`ssh-keygen -R <ip>`) — Hetzner recycles addresses within a project quickly, and the mismatch looks alarming out of context.
