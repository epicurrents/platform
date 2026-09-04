<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { t } from '#i18n'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'
import { validateViewerOverrides, type FieldValidation } from '#lib/viewerConfigValidator'
import { showToast } from '#lib/toast'

const SCOPE = 'ViewerConfigEditor'

const props = defineProps<{
    /** The persisted overrides; seeds the textarea and re-seeds when it changes. */
    overrides: ViewerSettingsOverrides
    /** Parent's persist call is in flight (drives the Save button's loading state). */
    saving?: boolean
    /** Disable editing while the parent loads. */
    disabled?: boolean
    /** Override the Save button label. */
    saveLabel?: string
}>()

const emit = defineEmits<{
    /** Emitted with the validated overrides once the editor clears parse + dry-validation. */
    (e: 'save', overrides: ViewerSettingsOverrides): void
}>()

/**
 * A parse failure: a message, plus — when the engine reports an in-range
 * offset — an extract of the input split around the offending character
 * (`mark`) so the editor can highlight it in context.
 */
interface ParseError {
    message: string
    before?: string
    mark?: string
    after?: string
}

const form = reactive({ overrides: '' })
const parseError = ref<ParseError | null>(null)
const validationIssues = ref<FieldValidation[]>([])
const validating = ref(false)

const saveText = computed(() => props.saveLabel ?? t('Save overrides', SCOPE))
const busy = computed(() => props.saving === true || validating.value)

function pretty (value: ViewerSettingsOverrides): string {
    return JSON.stringify(value, null, 2)
}

// Seed the textarea from the persisted overrides, and re-seed after the parent
// saves (it passes the server's canonical overrides back through the prop).
watch(() => props.overrides, value => {
    form.overrides = pretty(value)
}, { immediate: true })

// Clear stale errors once the user starts editing again.
watch(() => form.overrides, () => {
    parseError.value = null
    validationIssues.value = []
})

/**
 * Parse the overrides textarea into a flat settings map. An empty textarea
 * clears all overrides. Returns null and sets `parseError` when the text is
 * not a JSON object.
 */
function parseOverrides (): ViewerSettingsOverrides | null {
    const text = form.overrides.trim()
    if (!text) {
        return {}
    }
    let parsed: unknown
    try {
        parsed = JSON.parse(text)
    } catch (err) {
        parseError.value = describeParseError(text, (err as Error).message)
        return null
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        parseError.value = { message: t('The configuration must be a JSON object of field names to values.', SCOPE) }
        return null
    }
    return parsed as ViewerSettingsOverrides
}

/**
 * Turn a JSON.parse failure into a message and, when the engine reports an
 * in-range failure offset, an extract of the input split around the offending
 * character — so the user sees the problem character highlighted in context
 * instead of counting to a line and column.
 */
function describeParseError (text: string, rawMessage: string): ParseError {
    const offset = rawMessage.match(/position (\d+)/)
    const pos = offset ? Number(offset[1]) : -1
    if (pos < 0 || pos >= text.length) {
        // No in-range offset (e.g. unexpected end of input) — message only.
        return { message: t('Invalid JSON: {message}', SCOPE, { message: rawMessage }) }
    }
    // The highlighted extract replaces the trailing 'at position N (line/column)'.
    const cleaned = rawMessage.replace(/\s*(in JSON\s*)?at position.*$/i, '')
    const collapse = (value: string) => value.replace(/\s+/g, ' ')
    const mark = collapse(text.charAt(pos))
    let message = t('Invalid JSON: {message}', SCOPE, { message: cleaned })
    if (mark === "'") {
        message += '; ' + t('JSON uses double quotes around keys and string values, not single quotes.', SCOPE)
    }
    const RADIUS = 12
    const start = Math.max(0, pos - RADIUS)
    const end = Math.min(text.length, pos + RADIUS + 1)
    return {
        message,
        before: (start > 0 ? '…' : '') + collapse(text.slice(start, pos)),
        mark,
        after: collapse(text.slice(pos + 1, end)) + (end < text.length ? '…' : ''),
    }
}

/** Build the human-readable description of a rejected field. */
function describeIssue (issue: FieldValidation): string {
    if (issue.reason === 'type-mismatch') {
        return t('{field}: wrong value type (expected {type})', SCOPE, {
            field: issue.field,
            type: issue.expectedType ?? 'unknown',
        })
    }
    return t('{field}: not a known viewer setting', SCOPE, { field: issue.field })
}

async function save () {
    parseError.value = null
    validationIssues.value = []
    const overrides = parseOverrides()
    if (overrides === null) {
        return
    }
    validating.value = true
    try {
        // Dry-validate against a hidden viewer before handing off to the parent.
        // A null verdict means the validator viewer could not launch — proceed.
        const verdicts = Object.keys(overrides).length ? await validateViewerOverrides(overrides) : []
        if (verdicts === null) {
            showToast(t('Saved without validation: the viewer could not be reached.', SCOPE), 'warning')
        } else {
            const failures = verdicts.filter(verdict => !verdict.ok)
            if (failures.length) {
                validationIssues.value = failures
                showToast(t('Some settings are invalid and were not saved. Fix the issues listed and try again.', SCOPE), 'danger')
                return
            }
        }
        emit('save', overrides)
    } finally {
        validating.value = false
    }
}
</script>

<template>
    <div class="viewer-config-editor">
        <wa-callout v-if="parseError" variant="danger">
            {{ parseError.message }}
            <div v-if="parseError.mark !== undefined" class="viewer-config-editor__extract">
                <code class="viewer-config-editor__snippet">
                    <span>{{ parseError.before }}</span>
                    <span class="viewer-config-editor__mark">{{ parseError.mark }}</span>
                    <span>{{ parseError.after }}</span>
                </code>
            </div>
        </wa-callout>
        <wa-callout v-if="validationIssues.length" variant="danger">
            <strong>{{ t('Invalid settings', SCOPE) }}</strong>
            <ul class="viewer-config-editor__issues">
                <li v-for="issue in validationIssues" :key="issue.field">
                    {{ describeIssue(issue) }}
                </li>
            </ul>
        </wa-callout>
        <wa-textarea
            class="viewer-config-editor__textarea"
            :disabled="disabled"
            :label="t('Overrides (JSON)', SCOPE)"
            resize="vertical"
            rows="12"
            size="s"
            v-wa="[form, 'overrides']"
        ></wa-textarea>
        <wa-button
            appearance="filled-outlined"
            class="viewer-config-editor__save"
            :disabled="disabled"
            :loading="busy"
            variant="brand"
            @click="save"
        >
            {{ saveText }}
        </wa-button>
    </div>
</template>

<style scoped>
.viewer-config-editor {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

.viewer-config-editor__issues {
    margin: 0.25rem 0 0;
    padding-left: 1.25rem;
}

.viewer-config-editor__extract {
    margin-top: 0.4rem;
}

.viewer-config-editor__snippet {
    background: var(--wa-color-surface-lowered);
    border: 1px solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-s, 0.2rem);
    font-family: var(--wa-font-family-code, monospace);
    font-size: 0.85em;
    padding: 0.1rem 0.35rem;
    white-space: pre;
}

.viewer-config-editor__mark {
    background: var(--wa-color-danger-fill-loud);
    border-radius: 2px;
    color: white;
    padding: 0 0.1rem;
}

.viewer-config-editor__textarea {
    font-family: var(--wa-font-family-code, monospace);
}

.viewer-config-editor__save {
    align-self: flex-start;
}
</style>
