/**
 * The `meta` a HED-SCORE finding code is saved with, read from the viewer's own term index.
 *
 * Kept apart from the policy reads in `api.ts` because the index is viewer build output: a module naming it cannot be
 * loaded, or tested, where the viewer has not been built.
 */

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
