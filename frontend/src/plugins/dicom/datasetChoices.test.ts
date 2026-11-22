/**
 * The datasets a study is offered to: only those the user can add to, so the picker never leads to a refusal.
 */

import { describe, expect, it } from 'vitest'
import type { Collection } from '#api/library'
import { datasetChoicesFor } from './datasetChoices'

const dataset = (fields: Partial<Collection>) => ({
    author_id: 1,
    name: 'Set',
    object_hash: 'a'.repeat(32),
    release_gated: false,
    ...fields,
}) as Collection

describe('datasetChoicesFor', () => {
    it('offers the user their own datasets and not one they only read', () => {
        const choices = datasetChoicesFor(
            [dataset({ name: 'Mine' }), dataset({ author_id: 2, name: 'Shared with me' })], 1, false,
        )
        expect(choices).toEqual([{ hash: 'a'.repeat(32), name: 'Mine' }])
    })

    it('offers a superuser every dataset', () => {
        expect(datasetChoicesFor([dataset({ author_id: 2 })], 1, true)).toHaveLength(1)
    })

    it('leaves out a release-gated dataset and a row without a hash, for a superuser too', () => {
        const datasets = [dataset({ release_gated: true }), dataset({ object_hash: null })]
        expect(datasetChoicesFor(datasets, 1, false)).toEqual([])
        expect(datasetChoicesFor(datasets, 1, true)).toEqual([])
    })

    it('offers nothing without a signed-in user, whatever the author is recorded as', () => {
        expect(datasetChoicesFor([dataset({ author_id: null })], null, false)).toEqual([])
    })
})
