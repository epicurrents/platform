# Plugins

A **plugin** is a composable Django app under `plugins/<name>/` that adds
functionality alongside the active [project](../projects) without owning the
deployment's purpose. Unlike a project — of which exactly one is active per
deployment (`EPICURRENTS_PROJECT=<name>`) — zero or more plugins may be enabled
at once:

```
EPICURRENTS_PLUGINS=dicom,otherplugin
```

Projects and plugins differ in intent, not capability. A plugin can define
models, API endpoints, settings, Celery tasks, and frontend routes just like a
project; it simply must not claim the landing page or primary navigation UX.
The test for "project or plugin?" is: *would the same deployment plausibly want
a different one alongside it?* If yes, it is a plugin.

## Anatomy

| File | Required | Purpose |
|---|---|---|
| `apps.py` | Yes | [`PluginConfig`](../epicurrents/plugins.py) subclass; `default = True`, `name = "plugins.<name>"`, `label = "<name>"`. The explicit `default` is load-bearing: without it Django's config auto-detection is ambiguous (PluginConfig is imported into the module) and silently instantiates the bare `AppConfig`, so `ready()` never runs |
| `settings.py` | No | Extra / overriding Django settings, merged between `common` and the active project |
| `models.py` + `migrations/` | No | Extra models; migrations apply when the plugin is enabled |
| `urls.py` | No | Ninja API, mounted at `/plugin/<namespace>/api/v1/`; must expose `urlpatterns = [path("", api.urls)]` |
| `public_urls.py` | No | Plain Django URLs, mounted at `/plugin/<namespace>/` |
| `signals.py` / `tasks.py` / `management/commands/` | No | Standard Django app extension points |

The `/plugin/<namespace>/api/v1/` mount is recognised by the audit-trail
middleware, so plugin Ninja endpoints create `Activity` rows and their model
writes reach `ObjectChangeLog` like any core app's. The same platform
obligations follow: route session writes through a `_require_auth` helper that
calls `enforce_session_csrf`, and annotate each endpoint with a
`<plugin>.<resource>.<action>` verb via `log_activity`. The `public_urls.py`
slot is *not* audited (same as the project slot — see AGENTS.md).

The frontend half lives under `frontend/src/plugins/<name>/` and contributes
nav links, routes, icons, and viewer hooks through the same `ViewerPlugin`
contract that projects use ([frontend/src/projects/types.ts](../frontend/src/projects/types.ts)). It is compiled in
by listing the plugin in the `VITE_PLUGINS` build variable; registration in
[frontend/src/plugins/active.ts](../frontend/src/plugins/active.ts) pairs each
plugin with a `__PLUGIN_<NAME>__` build-time flag (defined in
[frontend/vite.config.ts](../frontend/vite.config.ts)) so disabled plugins
tree-shake out of the bundle.

## The `PluginConfig` contract

Beyond the standard `AppConfig` attributes, `PluginConfig` adds:

- `plugin_url_namespace: str | None` — mount segment override. Defaults to the
  short name, so `plugins.dicom` mounts at `/plugin/dicom/`.
- `requires: list[str]` — core apps and other plugins this plugin depends on. A
  required *plugin* must also appear in `EPICURRENTS_PLUGINS`, or the platform
  refuses to start.

## Loading and validation

[`epicurrents/plugin_loader.py`](../epicurrents/plugin_loader.py) drives loading
in two phases:

1. **Settings time** — `apply_plugin_settings` registers each `plugins.<name>`
   app and merges its `settings.py`. Precedence is
   `common < plugins < project < .env`.
2. **Apps-ready time** — `validate_plugins` (called from
   `EpicurrentsConfig.ready`) checks that every `requires` entry is satisfied
   and that no two plugins claim the same URL namespace. Violations raise
   `ImproperlyConfigured` at boot with a fix-it message.

URL mounting happens in [`epicurrents/urls.py`](../epicurrents/urls.py), which
walks `EPICURRENTS_PLUGINS` and mounts each plugin's `urls.py` /
`public_urls.py` before the SPA catch-alls. Modules are imported by directory
name; the mount segment is the config's resolved `url_namespace`, so a
`plugin_url_namespace` override changes the public paths without renaming the
directory.

## Enabling / disabling

```
scripts/enable_plugin.sh dicom     # adds to EPICURRENTS_PLUGINS, runs any submodule checkout, migrates
scripts/disable_plugin.sh dicom    # removes from EPICURRENTS_PLUGINS
```

See [docs/plugins.md](../docs/plugins.md) for the operator-facing walkthrough.

## Current plugins

| Plugin | README | Summary |
|---|---|---|
| `dicom` | [dicom/README.md](dicom/README.md) | DICOM upload / WADO / QIDO surface plus an OHIF viewer. |
