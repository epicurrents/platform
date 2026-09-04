# shellcheck shell=bash
# progress.sh — step-checklist output for the operator-facing shell scripts.
#
# Sourced, not executed. Provides a small API for scripts that run a known
# sequence of steps:
#
#   progress_step <id> <label> [direct]   Register a step in the plan.
#   progress_begin [title]                Lock the plan and start output.
#   run_step <id> <command...>            Run a registered step.
#   step_note <text>                      Set the step's one-line result note
#                                         (call from inside the step command).
#   progress_sudo_keepalive               Refresh the sudo timestamp in the
#                                         background for the rest of the run.
#
# On a terminal, the plan renders as a persistent checklist: completed steps
# collapse to a check mark, the active step shows a spinner, an elapsed-time
# counter, and a bounded tail of its live output, and pending steps are listed
# dimmed below. The frame is repainted in place with ANSI cursor movement, the
# same technique docker's build output uses, so it works in any VT100-family
# terminal — including over SSH, which forwards the TERM of an interactive
# session transparently.
#
# When stdout is not a TTY (piped to a file, CI, `ssh host cmd` without -t)
# the same API degrades to plain sequential output, one block per step.
# Force a mode with BOOTSTRAP_PROGRESS=plain or BOOTSTRAP_PROGRESS=fancy.
#
# Steps registered as `direct` run with their output flowing straight to the
# terminal in both modes: use this for steps that may prompt (sudo password)
# or whose output the operator must read. They are tagged `interactive` in
# the checklist to set them apart from the unattended steps. A direct step
# runs in the current shell, so it may mutate the caller's variables; a
# captured step runs in a background subshell and cannot.
#
# Captured step output is appended to $PROGRESS_LOG (default bootstrap.log in
# the current directory, overwritten per run). On step failure the last lines
# are echoed and the full path is printed.
#
# Bash 3.2 (macOS) and BSD coreutils compatible.

PROGRESS_LOG="${PROGRESS_LOG:-bootstrap.log}"
PROGRESS_TAIL_LINES="${PROGRESS_TAIL_LINES:-6}"

_PROGRESS_ESC=$'\033'
_PROGRESS_IDS=()
_PROGRESS_LABELS=()
_PROGRESS_STATE=()   # pending | active | done | fail
_PROGRESS_NOTE=()
_PROGRESS_DIRECT=()  # yes | no
_PROGRESS_PREV_LINES=0
_PROGRESS_SPIN_IDX=0
_PROGRESS_SPIN_CHARS='\|/-'
_PROGRESS_STEP_PID=""
_PROGRESS_KEEPALIVE_PID=""
_PROGRESS_CUR_LOG=""
_PROGRESS_STEP_T0=0
_PROGRESS_TMPDIR=""
_PROGRESS_BEGUN=false

# ── Message helpers (shared visual language for all modes) ──────────────────

bold()  { printf '\033[1m%s\033[0m\n'          "$*"; }
info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn()  { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()   { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Mode selection ───────────────────────────────────────────────────────────

case "${BOOTSTRAP_PROGRESS:-auto}" in
    plain) _PROGRESS_FANCY=false ;;
    fancy) _PROGRESS_FANCY=true ;;
    *)
        if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
            _PROGRESS_FANCY=true
        else
            _PROGRESS_FANCY=false
        fi
        ;;
esac

# ── Internals ────────────────────────────────────────────────────────────────

_progress_cols() {
    local c
    c="$(tput cols 2>/dev/null || echo 80)"
    if [ "${c:-0}" -lt 20 ]; then
        c=80
    fi
    printf '%s' "$c"
}

_progress_rows() {
    local r
    r="$(tput lines 2>/dev/null || echo 24)"
    if [ "${r:-0}" -lt 5 ]; then
        r=24
    fi
    printf '%s' "$r"
}

_progress_index_of() {
    local i
    for i in "${!_PROGRESS_IDS[@]}"; do
        if [ "${_PROGRESS_IDS[$i]}" = "$1" ]; then
            printf '%s' "$i"
            return 0
        fi
    done
    return 1
}

# Keep only the last carriage-return segment of each line (apt / docker
# progress bars), then strip ANSI CSI sequences. BSD awk / sed compatible;
# the ESC in the sed pattern is a literal byte.
_progress_strip_ctrl() {
    awk '{ n = split($0, a, "\r"); print a[n] }' \
        | sed -e "s/${_PROGRESS_ESC}\[[0-9;]*[a-zA-Z]//g"
}

# Repaint the checklist frame in place. The frame is always the last thing on
# screen; foreign output must go through _progress_frame_reset first.
_progress_render() {
    $_PROGRESS_FANCY || return 0
    local cols rows tail_max frame=() i line count extra j elapsed tag
    cols="$(_progress_cols)"
    rows="$(_progress_rows)"
    # Clamp the tail window so the frame always fits the terminal.
    tail_max=$((rows - ${#_PROGRESS_IDS[@]} - 2))
    if [ "$tail_max" -gt "$PROGRESS_TAIL_LINES" ]; then
        tail_max=$PROGRESS_TAIL_LINES
    fi
    _PROGRESS_SPIN_IDX=$(( (_PROGRESS_SPIN_IDX + 1) % 4 ))
    for i in "${!_PROGRESS_IDS[@]}"; do
        tag=""
        if [ "${_PROGRESS_DIRECT[$i]}" = "yes" ]; then
            tag=" ${_PROGRESS_ESC}[2m(interactive)${_PROGRESS_ESC}[0m"
        fi
        case "${_PROGRESS_STATE[$i]}" in
            done)
                frame+=(" ${_PROGRESS_ESC}[32m✓${_PROGRESS_ESC}[0m ${_PROGRESS_LABELS[$i]}${tag}${_PROGRESS_NOTE[$i]:+ ${_PROGRESS_ESC}[2m— ${_PROGRESS_NOTE[$i]}${_PROGRESS_ESC}[0m}")
                ;;
            fail)
                frame+=(" ${_PROGRESS_ESC}[1;31m✗${_PROGRESS_ESC}[0m ${_PROGRESS_LABELS[$i]}${tag}")
                ;;
            active)
                elapsed=$((SECONDS - _PROGRESS_STEP_T0))
                if [ "$elapsed" -ge 3 ]; then
                    elapsed=" ${_PROGRESS_ESC}[2m${elapsed}s${_PROGRESS_ESC}[0m"
                else
                    elapsed=""
                fi
                frame+=(" ${_PROGRESS_ESC}[36m${_PROGRESS_SPIN_CHARS:$_PROGRESS_SPIN_IDX:1}${_PROGRESS_ESC}[0m ${_PROGRESS_LABELS[$i]}${tag}${elapsed}")
                if [ -n "$_PROGRESS_CUR_LOG" ] && [ -s "$_PROGRESS_CUR_LOG" ] && [ "$tail_max" -gt 0 ]; then
                    while IFS= read -r line; do
                        line="${line:0:$((cols - 6))}"
                        frame+=("   ${_PROGRESS_ESC}[2m│ ${line}${_PROGRESS_ESC}[0m")
                    done < <(tail -n "$tail_max" "$_PROGRESS_CUR_LOG" | _progress_strip_ctrl)
                fi
                ;;
            *)
                frame+=(" ${_PROGRESS_ESC}[2m· ${_PROGRESS_LABELS[$i]}${_PROGRESS_ESC}[0m${tag}")
                ;;
        esac
    done
    count=${#frame[@]}
    if [ "$_PROGRESS_PREV_LINES" -gt 0 ]; then
        printf '%s[%dA' "$_PROGRESS_ESC" "$_PROGRESS_PREV_LINES"
    fi
    for i in "${!frame[@]}"; do
        printf '%s[2K%s\n' "$_PROGRESS_ESC" "${frame[$i]}"
    done
    # Blank any leftover lines from a taller previous frame.
    if [ "$count" -lt "$_PROGRESS_PREV_LINES" ]; then
        extra=$((_PROGRESS_PREV_LINES - count))
        for ((j = 0; j < extra; j++)); do
            printf '%s[2K\n' "$_PROGRESS_ESC"
        done
        printf '%s[%dA' "$_PROGRESS_ESC" "$extra"
    fi
    _PROGRESS_PREV_LINES=$count
}

# Finalize the current frame so foreign output (a direct step, a banner) can
# scroll below it. The next _progress_render starts a fresh frame.
_progress_frame_reset() {
    _PROGRESS_PREV_LINES=0
}

_progress_cleanup() {
    if [ -n "$_PROGRESS_KEEPALIVE_PID" ]; then
        kill "$_PROGRESS_KEEPALIVE_PID" 2>/dev/null || true
    fi
    if [ -n "$_PROGRESS_STEP_PID" ]; then
        kill "$_PROGRESS_STEP_PID" 2>/dev/null || true
    fi
    if $_PROGRESS_FANCY; then
        tput cnorm 2>/dev/null || true
    fi
    if [ -n "$_PROGRESS_TMPDIR" ]; then
        rm -rf "$_PROGRESS_TMPDIR"
    fi
}

_progress_interrupted() {
    _progress_frame_reset
    printf '\nInterrupted. Partial step output is in %s\n' "$PROGRESS_LOG" >&2
    exit 130
}

# ── Public API ───────────────────────────────────────────────────────────────

# progress_step <id> <label> [direct] — add a step to the plan. Steps run in
# registration order. `direct` marks the step interactive: full passthrough
# output, current-shell execution.
progress_step() {
    _PROGRESS_IDS+=("$1")
    _PROGRESS_LABELS+=("$2")
    _PROGRESS_STATE+=("pending")
    _PROGRESS_NOTE+=("")
    if [ "${3:-}" = "direct" ]; then
        _PROGRESS_DIRECT+=("yes")
    else
        _PROGRESS_DIRECT+=("no")
    fi
}

# progress_begin [title] — lock the plan, set traps, and print the run header.
# In plain mode also prints the plan as a numbered list, since there is no
# persistent frame to show upcoming steps.
progress_begin() {
    _PROGRESS_BEGUN=true
    _PROGRESS_TMPDIR="$(mktemp -d)"
    trap _progress_cleanup EXIT
    trap _progress_interrupted INT TERM
    if [ -n "${1:-}" ]; then
        bold "$1"
    fi
    if $_PROGRESS_FANCY; then
        : > "$PROGRESS_LOG"
        tput civis 2>/dev/null || true
        _progress_render
    else
        local i tag
        echo "Steps for this run:"
        for i in "${!_PROGRESS_IDS[@]}"; do
            tag=""
            if [ "${_PROGRESS_DIRECT[$i]}" = "yes" ]; then
                tag=" (interactive)"
            fi
            printf '  %d. %s%s\n' "$((i + 1))" "${_PROGRESS_LABELS[$i]}" "$tag"
        done
    fi
}

# step_note <text> — record the step's one-line result, shown after the label
# in the checklist and in the plain-mode completion line. Works from captured
# subshells and direct steps alike via a per-step note file.
step_note() {
    if [ -n "${_PROGRESS_NOTE_FILE:-}" ]; then
        printf '%s' "$*" > "$_PROGRESS_NOTE_FILE"
    fi
}

# run_step <id> <command...> — execute a registered step and track its state.
# A failing captured step echoes the tail of its log and exits the script. A
# direct or plain-mode step runs unguarded in the current shell, so a failure
# aborts through `set -e` with its output already on the terminal.
run_step() {
    local id="$1" idx label note_file rc=0 log
    shift
    idx="$(_progress_index_of "$id")" \
        || die "run_step: step '$id' is not registered"
    $_PROGRESS_BEGUN || die "run_step: call progress_begin first"
    label="${_PROGRESS_LABELS[$idx]}"
    note_file="$_PROGRESS_TMPDIR/note-$id"
    _PROGRESS_STATE[idx]="active"
    _PROGRESS_STEP_T0=$SECONDS

    if $_PROGRESS_FANCY && [ "${_PROGRESS_DIRECT[$idx]}" != "yes" ]; then
        log="$_PROGRESS_TMPDIR/step-$id.log"
        : > "$log"
        _PROGRESS_CUR_LOG="$log"
        _PROGRESS_NOTE_FILE="$note_file" "$@" > "$log" 2>&1 &
        _PROGRESS_STEP_PID=$!
        while kill -0 "$_PROGRESS_STEP_PID" 2>/dev/null; do
            _progress_render
            sleep 0.2
        done
        wait "$_PROGRESS_STEP_PID" || rc=$?
        _PROGRESS_STEP_PID=""
        _PROGRESS_CUR_LOG=""
        {
            printf -- '── %s ──\n' "$label"
            cat "$log"
        } >> "$PROGRESS_LOG"
        if [ "$rc" -ne 0 ]; then
            _PROGRESS_STATE[idx]="fail"
            _progress_render
            _progress_frame_reset
            printf '\n\033[1;31mERROR:\033[0m %s failed (exit %d). Last output:\n' "$label" "$rc" >&2
            tail -n 40 "$log" | _progress_strip_ctrl >&2
            printf 'Full output: %s\n' "$PROGRESS_LOG" >&2
            exit "$rc"
        fi
        _PROGRESS_STATE[idx]="done"
        if [ -s "$note_file" ]; then
            _PROGRESS_NOTE[idx]="$(cat "$note_file")"
        fi
        _progress_render
    else
        # Direct step, or plain mode: current shell, passthrough output.
        $_PROGRESS_FANCY && _progress_frame_reset
        info "$label"
        _PROGRESS_NOTE_FILE="$note_file" "$@"
        _PROGRESS_STATE[idx]="done"
        if [ -s "$note_file" ]; then
            _PROGRESS_NOTE[idx]="$(cat "$note_file")"
        fi
        ok "${_PROGRESS_NOTE[$idx]:-done}"
    fi
}

# progress_sudo_keepalive — refresh the sudo timestamp every minute so captured
# steps that use sudo never stall on an invisible password prompt. Call after
# a successful interactive sudo. -n never prompts; if the timestamp lapses
# anyway the refresh just fails silently.
progress_sudo_keepalive() {
    if [ -n "$_PROGRESS_KEEPALIVE_PID" ]; then
        return 0
    fi
    # Detached from the caller's stdio: the loop must not hold pipes open
    # past the script's exit when output is being captured.
    (
        while true; do
            sudo -n -v || true
            sleep 60
        done
    ) < /dev/null > /dev/null 2>&1 &
    _PROGRESS_KEEPALIVE_PID=$!
}
