import type { ViewerPlugin } from '../types'
import { getHedScorePolicy, hedCodeMeta } from './api'

/**
 * Viewer plugin for the *hedscore* plugin (findings coded in HED-SCORE).
 *
 * Contributes no routes and no picker: the viewer's annotation editor carries the finding picker and its terms. The
 * plugin gives it the deployment's policy, as a provider under `modules.eeg.hedScore.policy` that the picker calls
 * when it first opens, so the picker offers only what the server's validator accepts. A project's quick palette goes
 * beside it, as `modules.eeg.hedScore.palette` in the project's own `extraSetup`. A host saving a picked code stamps
 * it with `hedCodeMeta`, which the plugin provides as the `hed` entry of `codeMeta`.
 */
export const plugin: ViewerPlugin = {
    codeMeta: {
        hed: hedCodeMeta,
    },
    extraSetup: {
        modules: {
            eeg: {
                hedScore: {
                    policy: getHedScorePolicy,
                },
            },
        },
    },
}
