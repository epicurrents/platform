/**
 * The deployment's HED-SCORE policy, read for the viewer's finding picker.
 *
 * The picker ships its own terms; what the server says is which of them it accepts. Two reads answer that: the core
 * vocabulary listing says whether `hed` is registered at all, and the plugin's policy endpoint names the schema
 * release the validator pins and the branches it accepts. The `meta` a saved code carries comes from the client's own
 * index instead.
 */

import type { HedScorePolicy } from '@epicurrents/interface/modules/eeg'
import { http } from '#lib/http'

const VOCABULARIES_URL = '/annotations/api/v1/vocabularies'
const POLICY_URL = '/plugin/hedscore/api/v1/policy'

/** The standard a finding's code is stored under. */
const HED_STANDARD = 'hed'

interface VocabularyOut {
    standard: string
    label: string
    version: string
    closed: boolean
    confined: boolean
}

interface PolicyOut {
    library: string
    version: string
    with_standard: string
    sha256: string
    allowed_branches: string[]
}

/**
 * The policy the picker narrows itself to, or `null` when the server registers no `hed` vocabulary and takes no
 * finding codes. Rejects when either read fails, which the picker reports and retries the next time it opens.
 */
export async function getHedScorePolicy (): Promise<HedScorePolicy | null> {
    const vocabularies = await http.get<VocabularyOut[]>(VOCABULARIES_URL)
    if (!vocabularies.data.some(vocabulary => vocabulary.standard === HED_STANDARD)) {
        return null
    }
    const { data } = await http.get<PolicyOut>(POLICY_URL)
    return {
        allowedBranches: data.allowed_branches,
        schema: {
            library: data.library,
            sha256: data.sha256,
            version: data.version,
            withStandard: data.with_standard,
        },
    }
}

/**
 * The `meta` a `hed` code is sent with: the schema release of the term index the picker coded from. Taken from the
 * index rather than from the policy, so a client whose index is a different release than the server's pin is refused
 * rather than stamping the server's release on terms it did not check against. The index is imported on the first
 * call only.
 */
export async function hedCodeMeta () {
    const { hedSchemaMeta, loadHedScoreIndex } = await import('#root/viewer/epicurrents/eeg-module/dist/hed/index.js')
    return hedSchemaMeta((await loadHedScoreIndex()).schema)
}
