/**
 * Specs for the link between an attached item and its recording, which has to name the recording's current resource
 * whichever of the two loads first.
 */
import { describe, expect, it } from 'vitest'
import type { DataResource } from '@epicurrents/core/dist/types'
import { AttachmentParents } from './attachmentParents'

const resource = (name: string, parent?: DataResource | null) =>
    (parent === undefined ? { name } : { name, parent }) as unknown as DataResource & { parent?: DataResource | null }

describe('AttachmentParents', () => {
    it('links an item loaded after its recording to the loaded recording', () => {
        const parents = new AttachmentParents()
        const loaded = resource('EEG')
        parents.recordingHeld('aa', resource('EEG listing'))
        parents.recordingHeld('AA', loaded)
        const study = resource('US', null)
        parents.childLoaded('AA', study)
        expect(study.parent).toBe(loaded)
    })

    it('re-links an item loaded first when its recording loads', () => {
        const parents = new AttachmentParents()
        const listing = resource('EEG listing')
        parents.recordingHeld('AA', listing)
        const study = resource('US', null)
        parents.childLoaded('AA', study)
        expect(study.parent).toBe(listing)
        const loaded = resource('EEG')
        parents.recordingHeld('AA', loaded)
        expect(study.parent).toBe(loaded)
    })

    it('leaves alone a resource that takes no parent', () => {
        const parents = new AttachmentParents()
        parents.recordingHeld('AA', resource('EEG'))
        const document = resource('Report')
        parents.childLoaded('AA', document)
        expect('parent' in document).toBe(false)
    })
})
