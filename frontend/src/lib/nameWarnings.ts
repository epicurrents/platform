/**
 * Free-text warnings returned beside a write result.
 *
 * The backend runs a heuristic over display names, collection, dataset, folder and tag names
 * and the descriptions beside them (epicurrents/text_hygiene.py) and returns a warning per
 * field that reads as a personal name, an identifier or a date. The write has already
 * happened; this surfaces the warning so the user can look at the label again, since
 * everyone they share with sees it as typed.
 */

import { t } from '#i18n'
import { showToast } from '#lib/toast'

const SCOPE = 'nameWarnings'

/** One warning about a free-text field, as the backend returns it. */
export interface NameWarning {
    /** The request field the text came from (`display_name`, `name`, `description`, `prefix`). */
    field: string
    /** `digit_run`, `date`, `person_name`, or a deployment-defined kind. */
    kind: string
    /** English sentence for clients without their own wording. */
    message: string
}

function reason (kind: string): string {
    switch (kind) {
        case 'digit_run': {
            return t('contains a long run of digits, the usual shape of a record number or personal identifier', SCOPE)
        }
        case 'date': {
            return t('contains something that reads as a date', SCOPE)
        }
        case 'person_name': {
            return t('reads as a personal name', SCOPE)
        }
        default: {
            return t('matches a pattern this deployment flags', SCOPE)
        }
    }
}

/** The field a warning names, in the user's language; an unknown field token is shown with its underscores spaced. */
export function fieldLabel (field: string): string {
    switch (field) {
        case 'description': {
            return t('description', SCOPE)
        }
        case 'display_name': {
            return t('display name', SCOPE)
        }
        case 'name': {
            return t('name', SCOPE)
        }
        case 'prefix': {
            return t('name prefix', SCOPE)
        }
        case 'public_source': {
            return t('published source', SCOPE)
        }
        default: {
            return field.replace(/_/g, ' ')
        }
    }
}

/** Show one warning toast per flagged field; a no-op when there are none. */
export function toastNameWarnings (warnings: NameWarning[] | undefined): void {
    for (const warning of warnings ?? []) {
        showToast(
            t('The {field} {reason}. Everyone you share with sees it as typed.', SCOPE, {
                field: fieldLabel(warning.field),
                reason: reason(warning.kind),
            }),
            'warning',
        )
    }
}
