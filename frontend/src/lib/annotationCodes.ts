/**
 * The codes of a viewer annotation as the annotations API takes them inline.
 *
 * The viewer keeps an annotation's codes as one value per standard and nothing else, and saves nothing to the
 * platform. A host that saves an event or a label sends its codes in the write's own `codes`, each with the `meta` its
 * vocabulary pins, which the vocabulary's owner provides through `ViewerPlugin.codeMeta`. A code whose standard the
 * deployment does not register is left out, so one code the deployment cannot validate does not refuse the whole
 * write under the strict vocabulary setting; the server validates every code it is sent.
 *
 * @package epicurrents-frontend
 */

import type { CodeMetaProvider } from '#projects/types'
import { http } from '#lib/http'

const VOCABULARIES_URL = '/annotations/api/v1/vocabularies'

/** One code as a write sends it inline. */
export interface InlineCode {
    value: string
    meta: unknown
}

let providers: Record<string, CodeMetaProvider> = {}
let registered: Promise<Set<string>> | null = null

/**
 * Register the `meta` providers of the active project and plugins. The first to name a standard keeps it.
 * @param sources - The providers, in descending order of precedence.
 */
export function registerCodeMetaProviders (...sources: (Record<string, CodeMetaProvider> | undefined)[]) {
    providers = {}
    for (const source of sources) {
        for (const [standard, provider] of Object.entries(source ?? {})) {
            if (!(standard in providers)) {
                providers[standard] = provider
            }
        }
    }
}

/** The standards the deployment registers, read once per page and read again after a failed read. */
function registeredStandards () {
    if (!registered) {
        registered = http.get<{ standard: string }[]>(VOCABULARIES_URL).then(
            response => new Set(response.data.map(vocabulary => vocabulary.standard))
        )
        registered.catch(() => {
            registered = null
        })
    }
    return registered
}

/**
 * A viewer annotation's codes as the `codes` of an event or label write: one entry per registered standard, each with
 * the `meta` its provider answers, or `null` where none is provided. Rejects when the vocabulary listing or a provider
 * fails, so the caller retries the write rather than saving the annotation without its codes.
 * @param codes - The annotation's codes, keyed by standard.
 */
export async function inlineCodes (codes: Record<string, number | string> | null | undefined) {
    const inline: Record<string, InlineCode> = {}
    const entries = Object.entries(codes ?? {}).filter(([, value]) => value !== '' && value !== null)
    if (!entries.length) {
        return inline
    }
    const standards = await registeredStandards()
    for (const [standard, value] of entries) {
        if (!standards.has(standard)) {
            continue
        }
        const provider = providers[standard]
        inline[standard] = { value: String(value), meta: provider ? await provider() : null }
    }
    return inline
}

/** Forget the registered standards and providers; for tests. */
export function resetAnnotationCodes () {
    providers = {}
    registered = null
}
