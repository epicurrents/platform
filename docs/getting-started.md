# Getting started

This guide is for new users setting up Epicurrents for the first time, developers cloning the source to work on the platform, or anyone starting their own project plugin. It assumes basic familiarity with the command line.

If you're working with an AI assistant, point it at this file and ask for help — it will read the section that matches your situation and walk you through it one step at a time, rather than dumping the whole guide at you. The assistant's behaviour rules are in [AGENTS.md](../AGENTS.md).

## Before you begin

What you need on your machine:

- A terminal (Linux, macOS, or Windows with WSL).
- **Docker Engine** with **Docker Compose v2** (any current Docker release; the compose file uses recent Compose features such as `depends_on` conditions and the `!override` tag). On a fresh Ubuntu host, `scripts/bootstrap.sh` installs a current version for you.
- **Git** with submodule support (any modern Git release).

What you'll end up with depends on which path you're on. The most common arrangement is a single Docker Compose stack — web server, background worker, scheduler, PostgreSQL, Redis — all running on one host.

### Where AI assistance fits in

If you're using an AI assistant, three modes are practical, in roughly increasing order of independence from local setup:

- **Local clone + local AI** (Claude Code, Cursor, Aider, Copilot in your editor) — the assistant reads files from your checkout and can run commands directly. Most powerful mode; requires you to clone the repository first.
- **Remote AI + local execution** (ChatGPT, Claude.ai, Gemini in a browser with web access) — point the assistant at [AGENTS.md on GitHub](https://github.com/epicurrents/platform/blob/main/AGENTS.md). It reads the docs remotely and tells you what to run; you type the commands on your own machine. Works for the bootstrap phase without any local AI tooling.
- **Cloud dev environment** (GitHub Codespaces or similar) — open the repo on GitHub, click "Open in Codespace", and Copilot / Claude inside the Codespace gets filesystem access from the start. You install nothing locally. Docker-in-Docker inside Codespaces has its own quirks but covers most of what the platform needs.

What none of these modes do is clone the repository on your behalf without input — `git clone` (or its Codespaces equivalent) is always step 1 of any path below.

## Which path are you on?

| Your situation | Section to follow |
|---|---|
| Setting up a brand-new server for an institutional or research deployment | [Fresh deployment](#fresh-deployment) |
| Cloning the source code so you can develop against the platform | [Cloning the source for development](#cloning-the-source-for-development) |
| Adding your own customisation layer (extra models, endpoints, EDF middleware…) | [Starting a new project plugin](#starting-a-new-project-plugin) |
| Packaging the platform to share — a demo, or a distribution for one project | [Packaging a distribution to share](#packaging-a-distribution-to-share) |
| Restoring a crashed or degraded system (operator, no code knowledge needed) | [docs/operator-runbook.md](operator-runbook.md) |
| Diagnosing a specific problem with a running install (developer) | [docs/troubleshooting.md](troubleshooting.md) |
| Debugging why a background job or upload failed (developer) | [docs/debugging.md](debugging.md) |
| Day-to-day operations on a running install | [docs/operations.md](operations.md) |
| Just exploring the codebase | [Repository orientation in AGENTS.md](../AGENTS.md#repository-orientation) |

The three setup paths share most of the early steps; the differences kick in after the environment is initialised.

## Fresh deployment

For setting up a brand-new server. The audience is someone preparing an institutional or research deployment from scratch.

The whole flow is driven by [`scripts/bootstrap.sh`](../scripts/bootstrap.sh), which goes from a clean machine (git + docker installed — and on Ubuntu, bootstrap installs docker itself) to a running stack in two passes with a single pause for editing `.env`.

### 0. Create a non-root user

Skip this if you already log in as one. It matters on a cloud VM, where the stock Ubuntu image gives you `root` and nothing else — and `bootstrap.sh` refuses to run as root, because a stack whose files are owned by root is one an unprivileged container cannot read.

```bash
adduser --disabled-password --gecos "" epicurrents
usermod -aG sudo epicurrents
install -d -m 700 -o epicurrents -g epicurrents /home/epicurrents/.ssh
cp /root/.ssh/authorized_keys /home/epicurrents/.ssh/
chown epicurrents:epicurrents /home/epicurrents/.ssh/authorized_keys
```

If the account has no password — which it does not, with `--disabled-password` — `sudo` has nothing to prompt for and an unattended run will hang on it. Either give the account a password, or grant it passwordless sudo:

```bash
echo "epicurrents ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/epicurrents
chmod 440 /etc/sudoers.d/epicurrents
```

Then log back in as that user before continuing. Everything below assumes you are not root.

### 1. Get the code

```bash
git clone https://github.com/epicurrents/platform epicurrents
cd epicurrents
```

Submodules are initialised by `bootstrap.sh` in the next step. Two are checked out by default — the viewer and the vendored documentation. A third, dicom's OHIF viewer, is declared with `update = none` in [.gitmodules](../.gitmodules) and skipped unless the dicom project needs it.

### 2. Run bootstrap.sh (first pass)

```bash
chmod +x scripts/bootstrap.sh
./scripts/bootstrap.sh
```

On a fresh Ubuntu host this installs Docker Engine and Docker Compose from Docker's apt repositories and adds your user to the `docker` group. On macOS / Windows / non-Ubuntu Linux, install Docker Desktop manually first; the script will detect docker and skip the install step.

It then initialises the submodules, builds the Python image, and generates `.env` with securely-generated secrets (`SECRET_KEY`, `BORG_PASSPHRASE`, `ADMIN_PASSWORD`, VAPID keypair, federation Ed25519 keypair). **Each generated secret is printed to the console once, under `Generated secrets:`, and never shown again** — they remain readable in `.env`, which is the only copy. Resist the reasonable instinct to capture this run into a log: a `tee` or a redirect writes the database password, the Borg passphrase and the rest into a plaintext file that nothing cleans up. If you already have, delete it, and remember that terminal scrollback is the same exposure in a smaller way. After writing `.env` the script exits so you can review and customise it.

### 3. Edit `.env`

Open `.env` in your editor and fill in:

| Variable | What to set |
|---|---|
| `ALLOWED_HOSTS` | Comma-separated list of domain names this server will respond to (e.g. `eeg.example.com,localhost`). |
| `DB_*` | PostgreSQL credentials. Defaults work for development but should be changed for production. |
| `ADMIN_USERNAME`, `ADMIN_EMAIL` | Initial superuser. `ADMIN_PASSWORD` was generated by `init_env`, printed once during setup, and left in this file — note it down, then change it after first login (see [First login](#6-first-login)). |
| `EMAIL_*` | SMTP server details if you want password-reset emails to work. The default (`console.EmailBackend`) just logs them to stdout. |
| `PROXY_DOMAIN`, `PROXY_ACME_EMAIL` | Public hostname this deployment answers on, and a contact address for certificate-expiry warnings. Setting these turns on the bundled TLS proxy — see [Exposure and TLS](#exposure-and-tls) below. Leave empty when something else terminates TLS. |
| `EPICURRENTS_PROJECT` | Project plugin to activate, if any. Leave blank for the base platform. Set this *before* the next step — the `migrate` service applies the project's migrations automatically when the stack first comes up. |
| `EPICURRENTS_PROJECT_REPO` | Where to clone that project from. Projects live in their own repositories, so a fresh checkout of the platform has none; the second bootstrap pass clones this into `projects/<name>/` before it builds anything, since both the image and the frontend bundle are built from the project's own files. A bare name means the epicurrents org over HTTPS, `org/name` means that org, and a full `https://` or `git@host:` URL or a local path is used as given. Only needed when `EPICURRENTS_PROJECT` is set and the directory is not already there. |

Most other variables have sensible defaults.

> If you already have the project checked out — a developer working on it, or a deployment restored from a backup — put it at `projects/<name>/` and leave `EPICURRENTS_PROJECT_REPO` blank. Bootstrap only clones when the directory is missing, and never touches one that exists.

> You don't need to run `activate_project` for a fresh deployment — that command exists for *switching* between projects on an already-running deployment (see [`scripts/switch_project.sh`](../scripts/switch_project.sh)). On a first-time install, simply having `EPICURRENTS_PROJECT` set in `.env` is enough; the `migrate` service picks it up on the first `up -d`.

### 4. Run bootstrap.sh (second pass)

```bash
./scripts/bootstrap.sh
```

The second invocation skips everything that's already done (image build is cached, `.env` exists), builds the frontend bundles via the on-demand `frontend-build` Node container, initialises the local Borg backup repository, and starts the stack using the production compose overlay. Migrations (including any project plugin's migrations) and the initial admin user are created by the `migrate` service on first start.

Use `--no-start` to stop short of `docker compose up -d` (e.g. when you want to inspect the bundles first).

#### Exposure and TLS

The production overlay binds `${HOST_PORT:-8000}` on all interfaces, where the development stack binds `127.0.0.1` only. Gunicorn speaks plain HTTP and production sets `SECURE_SSL_REDIRECT`, so an internet-reachable host needs something in front of it that terminates TLS — without it, every request is redirected to an `https://` URL nothing is listening on.

Three ways to cover that:

**Let the stack do it.** Set `PROXY_DOMAIN` and `PROXY_ACME_EMAIL` in `.env` and [docker-compose.proxy.yml](../docker-compose.proxy.yml) joins the compose file list; `bootstrap.sh` and `update.sh` both select it from the presence of a `PROXY_DOMAIN` value, so there is no extra flag to remember. A Caddy container then terminates TLS on 80/443 with certificates it obtains and renews from Let's Encrypt itself, serves the static asset trees straight from disk, and proxies everything else to `web` — whose port binding drops back to loopback, making the proxy the only route in. Two preconditions, both checked the hard way if you skip them: the domain's A/AAAA record must already resolve to this host, and ports 80 and 443 must be reachable from the internet. The ACME HTTP-01 challenge runs on port 80 and fails without both.

**Join a tailnet.** No public port at all — Tailscale issues the certificate and proxies from your tailnet to `web`. See step 7.

**Front it with an ingress you already run.** An institutional reverse proxy or a cloud load balancer. Leave `PROXY_DOMAIN` empty, keep the direct binding, restrict `${HOST_PORT}` at the firewall to the proxy's source range, and set `TRUSTED_PROXIES` to that range so Django trusts the forwarded scheme and the security log attributes real client IPs rather than the proxy's.

HSTS starts at a deliberately short 300 seconds under all three. Raise it only after the certificate has renewed at least once on the real domain — see [operations.md → Security headers](operations.md#security-headers) for the ramp and the reasoning.

### 5. Verify

Open the URL configured in `ALLOWED_HOSTS` — `https://$PROXY_DOMAIN` with the bundled proxy, otherwise `http://localhost:8000` for a local install. Log in with the admin credentials from step 3. You should see the empty recording list.

To check the API is healthy:

```bash
curl http://localhost:8000/api/v1/ready
# {"status": "ready", "checks": {"database": "ok", "cache": "ok"}}

curl http://localhost:8000/api/v1/health
# {"status": "ok", "mode": "production", "debug": false}
```

`/ready` is what the `web` container's own healthcheck polls: it opens a database cursor and reads from the cache, and answers 503 with the failing dependency marked `"error"` if either does not respond. `/health` reports only that the process is answering — the `mode` and `debug` fields reflect `DJANGO_MODE`, and a development stack reports `"mode": "development", "debug": true` instead.

### 6. First login

The first `docker compose up -d` creates the superuser from `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL`. Open the site URL and sign in with those.

Then do both of the following, because nothing does them for you.

**Change the password.** The generated one was printed to your terminal once during setup and is still sitting in `.env` in plain text, next to every other secret. Change it from the profile page. `createadmin` only ever creates a missing account and never resets an existing one, so the stale value left in `.env` stops mattering the moment you have changed it.

**Enrol a second factor.** Two-factor authentication is opt-in per account and off until you turn it on, so the account with the most authority over the deployment has none by default. Enrol from the profile page with any TOTP authenticator. Store the recovery codes shown when you confirm the enrolment — that is the only time they are displayed, and they are the way back in if you lose the authenticator.

Once your own account holds a factor, decide whether to require one. `TWO_FACTOR_REQUIRED_FOR_STAFF` covers accounts with staff or superuser rights, and `TWO_FACTOR_REQUIRED_FOR_ALL` covers every account that signs in with a password; both default off, and both are documented in `.env`. Turning either on locks nobody out — an account without a factor is sent to enrolment during login and completes it in the same step that opens the session.

Neither applies to external (OIDC) logins, where the identity provider owns the second factor. If you configure external login later, set the requirement at the provider instead.

Creating further accounts has no in-app surface yet: use the account endpoints under `/api/v1/user/admin/` ([user/README.md](../user/README.md#account-administration)) or the management commands. A management UI is the first item on [ROADMAP.md](../ROADMAP.md).

### 7. (Optional) Reach the deployment over a tailnet

Instead of exposing a public port, you can put the deployment on a [Tailscale](https://tailscale.com) tailnet and reach it privately over WireGuard. This is separate from the [optional Tailscale layer for federation traffic](engineering-notes/federation-tailscale.md), which restricts server-to-server federation to a tailnet.

There are two arrangements, and picking the wrong one is the usual way this ends in an afternoon of debugging:

| | `join` (default) | `serve` |
|---|---|---|
| Script | [scripts/tailscale-join.sh](../scripts/tailscale-join.sh) | [scripts/tailscale-serve.sh](../scripts/tailscale-serve.sh) |
| What runs | The Tailscale package, on the host | An unprivileged container, compose profile `tailnet` |
| Direction | In and **out** | Inbound only |
| Containers can reach the tailnet | Yes, through the host | No |
| Host is modified | Yes | No |

Outbound is the asymmetry that matters. The container runs in userspace mode with no TUN device, so there is no route for a sibling container to take and the host gains no tailnet address of its own. Choose `join` for anything that has to leave this machine over the tailnet, including the [log shipping](#8-optional-ship-the-security-log-off-the-machine) in the next step.

There is a narrower way out of the container, and it is worth knowing rather than rediscovering: setting `TS_OUTBOUND_HTTP_PROXY_LISTEN` makes the same node run an HTTP proxy on the compose network, and a container pointed at it reaches the tailnet with no route of its own — the proxy resolves MagicDNS on its behalf too. It is verified, and it covers the log shipper. What it does not cover is anything that is not HTTP: remote Borg backups go over `ssh://`, so a deployment using both still needs the host on the tailnet. Each client also has to name the proxy in its own configuration, because an `HTTP_PROXY` environment variable is not universally honoured — promtail ignores it outright. Reach for it when installing packages on the host is not an option, not as the default.

Generate a single-use auth key in the [tailnet admin console](https://login.tailscale.com/admin/settings/keys), then either pass it to bootstrap:

```bash
./scripts/bootstrap.sh --tailscale-authkey tskey-... --tailscale-hostname my-deployment
./scripts/bootstrap.sh --tailscale-authkey tskey-... --tailscale-mode serve
```

or run either script any time after the stack is up:

```bash
./scripts/tailscale-join.sh --authkey tskey-... --hostname my-deployment
# or keep the key out of shell history by passing it in the environment:
TS_AUTHKEY=tskey-... ./scripts/tailscale-serve.sh --hostname my-deployment
```

A deployment installed from a distribution tarball has no `scripts/` directory; both scripts are bundled at its root and `start.sh` takes the same flags:

```bash
./start.sh --tailscale-authkey tskey-... --tailscale-hostname my-deployment
```

The auth key is single-use and **never written to disk**; only the device hostname (`TS_HOSTNAME`, non-secret) is saved to `.env`. Re-running is safe either way — `serve` keeps the node identity in the `tailscale-state` volume, and `join` leaves an already-joined host joined rather than spending a second key on it.

`join` also disables Tailscale's stateful filtering, without which the node drops the replies to a container's outbound traffic: the container NATs out through the host, so the answer comes back looking unsolicited. Everything works when tested from the host and nothing works from a container, which is a slow thing to discover.

For HTTPS to work end to end under `serve`, enable **HTTPS Certificates** in the tailnet admin console, add the node's MagicDNS name (`<hostname>.<your-tailnet>.ts.net`) to `ALLOWED_HOSTS`, and set `TRUSTED_PROXIES` (to the Docker network range) or `USE_X_FORWARDED_PROTO=True` so Django trusts the proxy's forwarded scheme and Secure cookies keep working. Check status with:

```bash
docker compose --profile tailnet exec tailscale tailscale status   # serve
sudo tailscale status                                              # join
```

### 8. (Optional) Ship the security log off the machine

An intruder who reaches this host can edit its logs, so the record of what they did is only worth what its independence is worth. [examples/evidence-host/](../examples/evidence-host/) is the arrangement that fixes that: the `epicurrents.security` stream is pushed as it is written to a second, separately-administered machine that keeps it, alerts on it, and — the part with no local equivalent — notices when this host goes quiet.

Only that one stream is sent. Application logs carry filesystem paths and tracebacks, and paths on the data volumes can embed subject identifiers; the security stream is the one channel with a standing no-raw-identifiers rule at every call site. The cost is forensic reach, since the logs that would reconstruct an incident in detail are still only on the host that was compromised.

The overlay ships with a distribution, under [examples/evidence-host/](../examples/evidence-host/), and needs the host on the tailnet the sink listens on — the `join` arrangement above, not `serve`. Building the receiving machine needs a checkout; the example's README covers both halves, and [docs/operations.md](operations.md) covers the queries and the alert rules.

If anything goes wrong, see [docs/troubleshooting.md](troubleshooting.md).

## Cloning the source for development

For developers who already have access to a running deployment (or want to run the test suite locally) and need a checkout of the code.

### 1. Clone with submodules

```bash
git clone https://github.com/epicurrents/platform epicurrents
cd epicurrents
git submodule update --init --recursive
```

### 2. Install Python dev dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt -r requirements-dev.txt
```

The `.venv` is git-ignored. You only need this if you want to run `pytest`, `ruff`, or other tooling on the host. Running the actual stack is still done via Docker Compose.

### 3. Run the test suite

```bash
pytest
```

The platform tests run against an in-memory SQLite database (no external services needed). See [Running tests](#running-tests) below for the PostgreSQL and project-specific test commands.

### 4. (Optional) Frontend-only development against a mock backend

You don't need a backend running to work on the Vue frontend:

```bash
cd frontend
cp .env.example .env
# Edit .env: set VITE_BACKEND_URL=mock
npm install
npm run dev
```

The mock dev server (defined in `frontend/mocks.ts`) provides an in-memory API with seeded recordings, collections, and datasets. State resets on every full page reload. See [`frontend/README.md`](../frontend/README.md) for the seed data details.

### 5. Run a real backend in Docker alongside frontend dev

If you want the frontend dev server to talk to a real backend, leave `VITE_BACKEND_URL` set to a URL like `http://localhost:8000`. Start the stack in another terminal:

```bash
docker compose up -d
```

The Vite dev server proxies API requests to it.

## Starting a new project plugin

For developers who have a working install and want to add their own customisation layer — extra models, API endpoints, EDF middleware, or settings overrides — without modifying the platform itself.

The scaffolded template at [`projects/example/`](../projects/example/) is heavily commented and works through the Django-side extension points, and carries a minimal frontend half. The two optional halves — a frontend and extra Python packages — are steps 7 and 8 below, and a project needing neither is complete without them.

### 1. Copy the template

```bash
cp -r projects/example projects/<yourname>
```

Replace `<yourname>` with a short identifier — alphanumeric, no spaces. The directory name becomes your project's Django app label.

**Your project is its own git repository, not a directory in the platform's.** `projects/<yourname>/` is where it lives at runtime, and every "commit it" below means committing to your repository, not the platform's:

```bash
cd projects/<yourname>
git init && git add . && git commit -m "initial commit"
```

Once it has a remote, set `EPICURRENTS_PROJECT_REPO` in `.env` on any other deployment and [`bootstrap.sh`](../scripts/bootstrap.sh) clones it for you. On this machine you already have the directory, so bootstrap leaves it as it is.

This is what keeps one deployment from carrying another's project: the code for a project you do not run is not on the machine, rather than present and unreferenced.

> The platform does not yet ignore `projects/`, because the projects that predate this arrangement are still tracked there. Until they move out, `git status` in the platform shows your nested repository as an untracked entry, and `git add -A` records it as a submodule reference rather than as files. Neither is harmful; leave it unstaged.

### 2. Edit `apps.py`

Open `projects/<yourname>/apps.py` and replace `example` with your project name in three places: the class name (`<Yourname>Config`), `name = "projects.<yourname>"`, and `label = "<yourname>"`.

```python
class MyprojectConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "projects.myproject"
    label = "myproject"
    requires_platform = ">=0.1,<0.2"

    def ready(self):
        pass
```

The label must not collide with any existing Django app label (`admin`, `auth`, `recordings`, `library`, etc.).

`requires_platform` is the range of platform versions your project supports — it is what keeps a project from silently drifting out of step with the platform it is checked out beside, now that the two are separate repositories. Rather than working the range out, ask the platform for it:

```bash
docker compose run --rm --no-deps web python -c \
    "from epicurrents.version import __version__, compatible_range; print(compatible_range(__version__))"
```

The cap is not simply the next major. The platform is on `0.x`, where semver makes the *minor* the breaking bump, so the range for 0.1.0 is `>=0.1,<0.2` — a cap of `<1` would accept every breaking release there is. That flips to the familiar `<2` shape once the platform reaches 1.0, which is why it is worth reading rather than assuming.

A platform outside the range stops the stack at `manage.py check` rather than misbehaving later; leaving the declaration out only warns, but then nothing is watching. See [epicurrents/README.md → Versioning and the platform pin](../epicurrents/README.md#versioning-and-the-platform-pin) for what a version actually promises, which is narrower than the whole codebase.

### 3. Define your models

Replace the contents of `projects/<yourname>/models.py` with your own models. Use string references (`"recordings.Recording"`) for cross-app foreign keys, and supply a `related_name` prefixed with your project name to avoid clashes:

```python
class ClinicalNote(models.Model):
    recording = models.OneToOneField(
        "recordings.Recording",
        on_delete=models.CASCADE,
        related_name="myproject_note",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
```

See [`projects/example/models.py`](../projects/example/models.py) for the full conventions including de-identification rules.

### 4. (Optional) Edit `settings.py`

If your project needs settings overrides — for example, a shorter recording retention window or a custom institution name — edit `projects/<yourname>/settings.py`. The merge rules are in [epicurrents/README.md](../epicurrents/README.md#settings-architecture):

- List settings (`INSTALLED_APPS`, `MIDDLEWARE`, etc.) are appended.
- `CELERY_BEAT_SCHEDULE` is merged.
- Everything else is replaced.

Delete the file entirely if you don't need overrides.

### 5. (Optional) Add API endpoints in `urls.py`

If your project needs HTTP endpoints, edit `projects/<yourname>/urls.py`. The `api` object is mounted at `/project/api/v1/` automatically. See [`projects/example/urls.py`](../projects/example/urls.py) for the patterns to follow (auth checks, atomicity, response schemas).

Delete the file entirely if you don't need endpoints.

For non-API content that doesn't fit under `/project/api/v1/` — an embedded viewer SPA, files needing custom HTTP headers (e.g. `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy` for WASM), or anything that needs to live outside the JSON-API namespace — add a `projects/<yourname>/public_urls.py` with plain Django URL patterns; it's mounted at `/project/<yourname>/`. Both files are optional and independent of each other.

### 6. (Optional) EDF middleware in `middleware.py`

If your project needs to transform EDF/BDF files on-the-fly (anonymise headers, drop channels, etc.), see [`projects/example/middleware.py`](../projects/example/middleware.py) for a worked example. The middleware system is documented in [federation/README.md](../federation/README.md#middleware-pipeline).

Delete the file entirely if you don't need middleware.

### 7. (Optional) A frontend of your own

A project can contribute UI — routes, nav links, icons, viewer setups — from `projects/<yourname>/frontend/index.ts`, which must export a `plugin` object satisfying `ViewerPlugin` ([frontend/src/projects/types.ts](../frontend/src/projects/types.ts)). The build resolves the `#project` alias to exactly that file, so only the active project's UI is ever in the bundle.

The template's `frontend/` is the smallest useful plugin — one route, one nav link, one icon — and the copy in step 1 brings it along; extend it, or delete the directory if your project has no UI. A project without one is a supported arrangement: the alias falls back to the base no-op plugin and the build succeeds. Keep the template's `package.json` when you keep the directory — a project frontend needs its own manifest re-declaring the platform's `imports` map, because Node resolves `#`-prefixed specifiers against the nearest manifest rather than the platform's.

A mistyped `VITE_PROJECT` is a build failure rather than a silent fallback, so a project whose UI is missing says so instead of quietly serving the base one.

### 8. (Optional) Python packages the platform doesn't have

If your project imports something outside the platform's dependencies, list it in `projects/<yourname>/requirements.txt` and generate the lock beside it:

```bash
scripts/lock-requirements.sh --project <yourname>
```

Commit both. The image build installs the lock after the platform's own, so a rebuild is what puts the packages in the container.

Generate the lock with that command rather than with `uv` or `pip freeze` directly. It resolves your project against the platform's exact locked versions, which is what stops a package you share with the platform — `numpy`, most commonly — from being quietly replaced at a different version when your lock installs over the platform's.

Re-run it whenever the platform's `requirements.lock` changes. You will not have to remember: your lock records which platform versions it was resolved against, and both `--check` and the image build refuse a pair that has drifted, naming the command to run.

Skip this step if the platform already has everything you import.

### 9. Generate the initial migration

```bash
EPICURRENTS_PROJECT=<yourname> docker compose run --rm --no-deps web python manage.py makemigrations <yourname>
```

This produces `projects/<yourname>/migrations/0001_initial.py`. Commit it.

### 10. Activate the project

Edit `.env` and set `EPICURRENTS_PROJECT=<yourname>`. Then:

```bash
docker compose run --rm --no-deps web python manage.py activate_project <yourname>
docker compose up -d
```

If the stack is already running, restart the application containers so they pick up the new project:

```bash
docker compose restart web celery celery-beat
```

### 11. Verify

Check that your project's endpoints respond at `/project/api/v1/`:

```bash
curl http://localhost:8000/project/api/v1/
```

For a more complete worked walkthrough of switching between projects (deactivate one, activate another), see [`scripts/switch_project.sh`](../scripts/switch_project.sh).

## Packaging a distribution to share

To hand the platform to someone else — a colleague who wants to try it, or a deployment of one project — build a self-contained package with [`scripts/make-bootstrap-fixture.sh`](../scripts/make-bootstrap-fixture.sh). It bundles the backend, a prebuilt UI, and optionally a project into a directory the recipient runs with nothing installed but Docker.

Build the frontend bundles first — the packages ship them prebuilt, not as source:

```bash
cd frontend && npm run build && npm run build:viewer && cd ..
```

Then assemble the package for your case:

```bash
# Base platform with a browsable UI — no signal viewer, no project:
scripts/make-bootstrap-fixture.sh ~/epicurrents-demo --demo

# A specific project — full experience (UI + signal viewer + the project active):
scripts/make-bootstrap-fixture.sh ~/epicurrents-myproject --dist --with-project myproject
```

The recipient runs the bundled start script and opens the printed URL; the package's own README walks them through it, and they need only Docker. On a bare Linux server that does not have it, the package's prepare-host.sh installs Docker, creates the uid-1000 account the containers run as and hands the deployment to it — the two things start.sh cannot do for itself, since it has to run as that unprivileged account. It installs Docker from the same [scripts/lib/install-docker.sh](../scripts/lib/install-docker.sh) that [scripts/bootstrap.sh](../scripts/bootstrap.sh) uses, so a packaged deployment and a cloned one get the same engine. `--demo` leaves the signal viewer out (base UI only); `--dist` includes it and activates the chosen project. Run the script with `--help` for every option, and see [docs/developing.md](developing.md#bootstrap-smoke-fixture) for how the same script also drives the CI smoke fixture.

## Running tests

Two test suites depending on what you're working on:

### Platform tests

```bash
pytest
```

The bare invocation is deliberate: [`conftest.py`](../conftest.py) decides what belongs to the platform suite. Project and plugin test trees whose settings module is not the active one are pruned from collection (they fail while importing, so no skip mark could save them), and tests needing a usable libfuse skip themselves via `require_fuse` instead of being ignored by path — so they run wherever the library is installed.

These run on in-memory SQLite. To run the same suite against PostgreSQL — which catches type bugs SQLite's dynamic typing hides — use the docker service:

```bash
docker compose run --rm test-postgres
```

See [docs/developing.md](developing.md#against-postgresql) for what it covers.

### Project plugin tests

For tests inside your own project plugin:

```bash
DJANGO_SETTINGS_MODULE=projects.<yourname>.settings_test pytest projects/<yourname>/tests/
```

The `DJANGO_SETTINGS_MODULE` override is needed because the platform's `pytest.ini` defaults to `epicurrents.settings.test_platform`, which doesn't have your project's models in `INSTALLED_APPS`. See [`projects/example/settings_test.py`](../projects/example/settings_test.py) for the scaffolded settings file to copy.

## Common first-day questions

**How do I log in for the first time?**
See [First login](#6-first-login) below — and do the two hardening steps there, because nothing does them for you.

**Where are recordings stored on disk?**
In the `recordings-data` named volume, mounted at the path in `RECORDINGS_UPLOAD_PATH` (`/data/recordings` in the stack). Files in flight during upload live briefly in `RECORDINGS_STAGING_PATH` (the `staging-data` volume at `/data/staging`). The stack uses a separate named volume per data domain — `recordings-data`, `staging-data`, `media-data`, `postgres-data`, `celery-data`, `borg-data` — defined at the bottom of [docker-compose.yml](../docker-compose.yml).

**Do I need PostgreSQL installed on my host?**
No. The Docker stack runs its own PostgreSQL. The host only needs Docker.

**Can I develop without Docker?**
Partially. Set `DJANGO_MODE=development` in `.env` to use SQLite + Django's runserver on the host. Some features (Celery, Redis caching, full middleware pipeline) need the Docker services to work fully. Frontend-only work doesn't need either — see [step 4 of Cloning](#4-optional-frontend-only-development-against-a-mock-backend).

**How do I run a management command?**
Use the helper script:

```bash
scripts/manage.sh <command> [args...]
# e.g.
scripts/manage.sh createsuperuser
scripts/manage.sh migrate
scripts/manage.sh rollback_change 42 --user-id 1
```

It's a thin wrapper around `docker compose run --rm web python manage.py`. **Always run management commands inside the Docker container, not on the host** — the host's SQLite database and the container's PostgreSQL are different, and running commands on the host can corrupt the Docker stack's state.

**I changed some code, what do I need to restart?**
- **Python file** (a view, a task) → `scripts/apply-changes.sh` or `docker compose restart web celery celery-beat`.
- **Model change** → first `docker compose exec web python manage.py makemigrations && docker compose exec web python manage.py migrate`, then restart.
- **Frontend file** → either let the Vite dev server hot-reload (if running) or `scripts/rebuild-frontend.sh`.
- **`.env` change** → `docker compose restart` for most variables; for some (e.g. `EPICURRENTS_PROJECT`) the full sequence of [`scripts/switch_project.sh`](../scripts/switch_project.sh).

The intent-keyed cookbook in [docs/operations.md](operations.md) covers more cases.

**How do I reset the local stack to a clean state?**

```bash
scripts/reset.sh
```

Refuses to run unless `DJANGO_MODE=development` is set in `.env`, so you can't accidentally wipe a production deployment. Removes all containers, all volumes, and all data. Optionally redeploys.

**Where do I find a specific management command?**
See [docs/management-commands.md](management-commands.md) for the full index of management commands across all apps.

**Where do I find the architecture details?**
Each Django app has a README under `<app>/README.md`. The "Backend apps" table in [AGENTS.md](../AGENTS.md#backend-apps) is the index.

## When something goes wrong

- **Symptoms and known issues** → [docs/troubleshooting.md](troubleshooting.md).
- **Operational tasks (logs, restarts, backups)** → [docs/operations.md](operations.md).
- **Architecture questions** → per-app READMEs (linked from [AGENTS.md](../AGENTS.md#backend-apps)).
- **Working with an AI assistant** → just describe the symptom or the goal. The assistant will read the relevant files and walk you through diagnosis.
