/**
 * The document kinds a contextual assessment reference can point at, and the prefix that names one.
 *
 * EDPB Guidelines 02/2026 name no reference vocabulary; a reference is a pointer to a document the
 * sharer keeps elsewhere. What the guidelines do let us deduce is what kind of document that is: the
 * written contextual assessment itself (paragraphs 45–49 and the Annex 1 flowchart), a data protection
 * impact assessment, an agreement with the recipient carrying a re-identification prohibition
 * (paragraphs 34 and 90), or a published dataset's own anonymity statement, which a sharer adopts as
 * their finding for a recipient who could obtain the same data from the publisher (paragraph 26: the
 * platform's copy adds no means). The SPA offers those as a selector and stores the choice as a fixed,
 * untranslated prefix inside `assessment_reference`, so the backend and the sweep output stay a single
 * free-text field and a reference typed anywhere else reads the same way.
 *
 * @package epicurrents-platform-frontend
 */

/** Stored prefix → the kind. The prefix is a stable English token, not a translated label. */
export const ASSESSMENT_KINDS = [
    { key: 'assessment', prefix: 'Contextual assessment' },
    { key: 'dpia', prefix: 'DPIA' },
    { key: 'agreement', prefix: 'Data-sharing agreement' },
    { key: 'published', prefix: 'Published dataset' },
] as const

export type AssessmentKind = (typeof ASSESSMENT_KINDS)[number]['key'] | ''

const SEPARATOR = ': '

/** Split a stored reference into the kind it was saved with, if any, and the identifier that follows. */
export function splitAssessmentReference(reference: string): { kind: AssessmentKind, identifier: string } {
    for (const kind of ASSESSMENT_KINDS) {
        const head = `${kind.prefix}${SEPARATOR}`
        if (reference.startsWith(head)) {
            return { kind: kind.key, identifier: reference.slice(head.length) }
        }
    }
    return { kind: '', identifier: reference }
}

/** Compose the stored reference from a kind and an identifier; an empty identifier gives an empty reference. */
export function joinAssessmentReference(kind: AssessmentKind, identifier: string): string {
    const trimmed = identifier.trim()
    if (!trimmed) {
        return ''
    }
    const match = ASSESSMENT_KINDS.find(k => k.key === kind)
    return match ? `${match.prefix}${SEPARATOR}${trimmed}` : trimmed
}
