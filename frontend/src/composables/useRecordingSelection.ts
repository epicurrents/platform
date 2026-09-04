import { ref, onMounted, onUnmounted } from 'vue'
import type { Ref } from 'vue'

/**
 * Manages multi-selection of recordings in a list, with checkbox, Ctrl/Cmd,
 * and Shift-range click support.
 *
 * @param orderedHashes  Function returning the current ordered list of
 *                       selectable hashes (used for shift-range calculation).
 * @param containerRef   Ref to the list container element. A mousedown listener
 *                       clears only the focused (unhighlighted, unchecked) row on
 *                       any click outside the container. Checked rows (selected)
 *                       are never cleared implicitly — use clearSelection for that.
 *
 * Interaction model:
 *   - Plain click (no selection active) → focus item (highlight + show unchecked checkbox)
 *   - Plain click (selection active)    → toggle item (same as checkbox click)
 *   - Ctrl/Cmd-click → toggle item in multi-selection set (checked checkbox)
 *   - Shift-click    → extend multi-selection range from last anchor
 *   - Double-click   → handled by the caller (onRowClick does NOT open on plain click)
 *   - Checkbox click → toggle multi-selection (same as Ctrl/Cmd-click)
 *   - Click outside  → clears focused row only; checked rows are unaffected
 *
 * onRowClick always returns 'select'; opening is triggered by double-click only.
 */
export function useRecordingSelection(
    orderedHashes: () => string[],
    containerRef?: Ref<HTMLElement | null>,
) {
    const selected = ref<Set<string>>(new Set())
    /** Hash of the single focused (plain-clicked) row; distinct from multi-selection. */
    const focusedHash = ref<string | null>(null)
    /** Hash of the last row that was explicitly clicked (anchor for shift-range). */
    const lastClickedHash = ref<string | null>(null)

    function toggle(hash: string): void {
        const s = new Set(selected.value)
        if (s.has(hash)) s.delete(hash)
        else s.add(hash)
        selected.value = s
    }

    function selectRange(from: string, to: string): void {
        const hashes = orderedHashes()
        const a = hashes.indexOf(from)
        const b = hashes.indexOf(to)
        if (a === -1 || b === -1) return
        const [lo, hi] = a <= b ? [a, b] : [b, a]
        const s = new Set(selected.value)
        for (let i = lo; i <= hi; i++) s.add(hashes[i]!)
        selected.value = s
    }

    /**
     * Handle a click on a row.
     * Always returns 'select'; callers should open recordings on double-click instead.
     */
    function onRowClick(hash: string, event: MouseEvent): 'select' {
        if (event.ctrlKey || event.metaKey) {
            toggle(hash)
            focusedHash.value = null
            lastClickedHash.value = hash
            return 'select'
        }
        if (event.shiftKey && lastClickedHash.value !== null) {
            selectRange(lastClickedHash.value, hash)
            focusedHash.value = null
            lastClickedHash.value = hash
            return 'select'
        }
        // Plain click while selection is active — toggle the item.
        if (selected.value.size > 0) {
            toggle(hash)
            focusedHash.value = null
            lastClickedHash.value = hash
            return 'select'
        }
        // Plain click with no active selection — focus item only.
        focusedHash.value = hash
        lastClickedHash.value = hash
        return 'select'
    }

    /**
     * Handle a click on the checkbox element.
     * Always toggles multi-selection without opening; callers should call event.stopPropagation().
     */
    function onCheckboxClick(hash: string): void {
        toggle(hash)
        focusedHash.value = null
        lastClickedHash.value = hash
    }

    function clearSelection(): void {
        selected.value = new Set()
        focusedHash.value = null
        lastClickedHash.value = null
    }

    if (containerRef !== undefined) {
        function handleOutsideMousedown(event: MouseEvent): void {
            if (!containerRef!.value?.contains(event.target as Node)) {
                // Only clear the focused (unhighlighted) row — checked rows are unaffected.
                focusedHash.value = null
            }
        }
        onMounted(() => document.addEventListener('mousedown', handleOutsideMousedown))
        onUnmounted(() => document.removeEventListener('mousedown', handleOutsideMousedown))
    }

    return { selected, focusedHash, onRowClick, onCheckboxClick, clearSelection }
}
