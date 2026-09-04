# Plugins

A **plugin** adds functionality to an Epicurrents deployment without defining
its purpose. Where a [project](../projects) is the single customisation layer
that owns a deployment's landing page and primary UX (`EPICURRENTS_PROJECT=<name>`),
a plugin composes *alongside* whatever project is active — and zero or more may
be enabled at once.

The canonical example is `dicom`: a DICOM upload / WADO / QIDO surface plus an
OHIF viewer. A clinic running a project for ICU EEG may also want
DICOM support for the same patients; the plugin model makes that possible where
the one-project-per-deployment model did not.

## Enabling a plugin

```bash
scripts/enable_plugin.sh dicom
```

This adds `dicom` to `EPICURRENTS_PLUGINS` in `.env` and `VITE_PLUGINS` in
`frontend/.env`, fetches any plugin-specific submodule (DICOM ships its OHIF
viewer as a `update = none` submodule), applies the plugin's migrations against
PostgreSQL, and rebuilds the frontend. Restart the stack afterward:

```bash
docker compose up -d
```

To enable several plugins, run the script once per plugin, or set the
comma-separated lists directly:

```dotenv
# .env
EPICURRENTS_PLUGINS=dicom,otherplugin
```

```dotenv
# frontend/.env
VITE_PLUGINS=dicom,otherplugin
```

The two lists must match, and both changes require a frontend rebuild
([scripts/rebuild-frontend.sh](../scripts/rebuild-frontend.sh)) to take effect.

## Disabling a plugin

```bash
scripts/disable_plugin.sh dicom
```

This removes the plugin from both lists and rebuilds the frontend. It does **not**
drop the plugin's database tables — disabling is reversible and a later
re-enable expects its data intact.

## What happens at startup

1. **Settings merge.** Each enabled plugin's `settings.py` is merged into Django
   settings between the base `common` settings and the active project's, so
   precedence is `common < plugins < project < .env`. A project always has the
   last word over a plugin it composes with.
2. **URL mounting.** Each plugin's `urls.py` mounts at
   `/plugin/<namespace>/api/v1/` and its `public_urls.py` at
   `/plugin/<namespace>/`. The namespace is the plugin's directory name unless
   its config sets `plugin_url_namespace`.
3. **Validation.** Before serving the first request, the platform checks that
   every plugin's declared dependencies are satisfied and that no two plugins
   claim the same URL namespace. A problem raises a clear startup error naming
   the offending plugin and the fix, rather than a 500 later.

Common startup errors and their fixes:

| Message | Cause | Fix |
|---|---|---|
| `plugins/<name>/ does not exist` | `EPICURRENTS_PLUGINS` names a directory that isn't there | Correct the name in `.env` |
| `Plugin 'B' requires 'A', which is neither a loaded core app nor an enabled plugin` | Plugin B depends on plugin A, which is not enabled | Add A to `EPICURRENTS_PLUGINS` |
| `Plugins 'X' and 'Y' both claim the URL namespace '/plugin/z/'` | Two plugins resolve to the same mount segment | Give one an explicit `plugin_url_namespace` |

## Authoring a plugin

Create a directory `plugins/<name>/` with at minimum an `apps.py`:

```python
from epicurrents.plugins import PluginConfig


class MyPluginConfig(PluginConfig):
    default = True  # required — see plugins/README.md
    name = "plugins.myplugin"
    label = "myplugin"
    requires: list[str] = []  # core apps / other plugins this depends on
```

Then add, as needed: `settings.py`, `models.py` + `migrations/`, `urls.py`
(Ninja API; expose `urlpatterns = [path("", api.urls)]`), `public_urls.py`
(plain Django), `signals.py`, `tasks.py`, `management/commands/`. Plugin API
endpoints carry the same platform obligations as core apps: session writes go
through a `_require_auth` helper that calls `enforce_session_csrf`, and every
endpoint annotates its `Activity` row via `log_activity`. The frontend half
lives under `frontend/src/plugins/<name>/` and exports a `ViewerPlugin` (same
contract as a project) registered in
[frontend/src/plugins/active.ts](../frontend/src/plugins/active.ts) behind a
`__PLUGIN_<NAME>__` build-time flag defined in `frontend/vite.config.ts`.

See [plugins/README.md](../plugins/README.md) for the in-repo developer contract
and [plugins/dicom/](../plugins/dicom/README.md) as the reference implementation.

## Project vs. plugin — which is it?

The test is: *would the same deployment plausibly want a different one
alongside it?* If yes, it is a plugin; if the thing defines what the deployment
is for, it is a project. A teaching project or a clinical annotation project
owns a deployment's purpose and is a project. `dicom` is an add-on and is a plugin.
