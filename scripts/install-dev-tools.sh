#!/usr/bin/env bash
# install-dev-tools.sh — set up the per-checkout developer tooling that is
# not tracked in git: project-scoped git hooks and tool-specific symlinks
# into the generic `.review/` directory.
#
# Idempotent — safe to re-run on every bootstrap invocation.  Invoked
# automatically by scripts/bootstrap.sh and is also the supported way to
# install the tooling manually after a fresh clone.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"

# ── Git hooks ──────────────────────────────────────────────────────────────

hooks_src="$repo_root/scripts/git-hooks"
hooks_dst="$repo_root/.git/hooks"

if [ ! -d "$hooks_src" ]; then
    echo "scripts/git-hooks/ does not exist; skipping git-hook install."
elif [ ! -d "$hooks_dst" ]; then
    echo "WARNING: .git/hooks does not exist — not in a git checkout?" >&2
else
    for hook_src in "$hooks_src"/*; do
        [ -e "$hook_src" ] || continue
        name="$(basename "$hook_src")"
        target="$hooks_dst/$name"
        if [ -L "$target" ] || [ -e "$target" ]; then
            rm "$target"
        fi
        ln -s "../../scripts/git-hooks/$name" "$target"
        chmod +x "$hook_src"
        echo "Installed git hook: .git/hooks/$name -> scripts/git-hooks/$name"
    done
fi

# ── AI-tool discovery symlinks ─────────────────────────────────────────────
#
# .review/ holds the tool-agnostic review-agent specs.  Individual AI
# assistants expect their own paths (Claude Code: .claude/agents/).  Create
# the tool-specific symlinks here so the substantive content lives in one
# place but each tool finds it under the path it expects.  These symlinks
# are not tracked in git — `.claude/agents` is gitignored — so they are recreated
# at every install.

# Claude Code — agents
claude_dir="$repo_root/.claude"
mkdir -p "$claude_dir"

claude_agents_link="$claude_dir/agents"
if [ -L "$claude_agents_link" ] || [ -e "$claude_agents_link" ]; then
    # Remove if existing — but only if it is a symlink or empty directory,
    # to avoid clobbering real files a contributor may have placed there.
    if [ -L "$claude_agents_link" ]; then
        rm "$claude_agents_link"
    elif [ -d "$claude_agents_link" ] && [ -z "$(ls -A "$claude_agents_link")" ]; then
        rmdir "$claude_agents_link"
    else
        echo "WARNING: $claude_agents_link exists and is not empty — leaving it alone."
        echo "         Remove it manually and re-run if you want the symlink."
        exit 0
    fi
fi
ln -s "../.review/agents" "$claude_agents_link"
echo "Installed symlink:  .claude/agents -> ../.review/agents"
