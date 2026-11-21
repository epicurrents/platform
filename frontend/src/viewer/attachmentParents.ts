/**
 * Links each attached item the viewer loads to the resource of the recording it is attached to.
 *
 * A listed item is replaced by another object when it loads, so a link set to a recording's listing would point at a
 * resource its dataset no longer holds. The tracker therefore keeps, per recording hash, the resource currently in
 * the dataset, and sets the link from whichever of the two loads last: an attached item loaded before its recording
 * is linked to the listing and re-linked when the recording loads. A resource that takes no parent (anything but a
 * radiology study today) is left alone.
 */

import type { DataResource } from '#epicurrents/core/dist/types'
import type { RadiologyResource } from '@epicurrents/interface/modules/rad'

/** True for a resource that names the resource it is attached to. */
function takesParent(resource: DataResource): resource is RadiologyResource {
    return 'parent' in resource
}

export class AttachmentParents {
    /** Loaded attached items, by the hash of the recording they are attached to. */
    #children = new Map<string, DataResource[]>()
    /** The resource each recording currently has in its dataset, by its hash. */
    #current = new Map<string, DataResource>()

    /**
     * Record an attached item once it has loaded, and link it to its recording.
     * @param parentHash - Hash of the recording the item is attached to.
     * @param resource - The loaded item.
     */
    childLoaded(parentHash: string, resource: DataResource) {
        if (!takesParent(resource)) {
            return
        }
        const key = parentHash.toUpperCase()
        this.#children.set(key, [...(this.#children.get(key) ?? []), resource])
        resource.parent = this.#current.get(key) ?? null
    }

    /**
     * Record the resource a recording has in its dataset, as listed or as loaded, and re-link its loaded items to it.
     * @param hash - Hash of the recording.
     * @param resource - The recording's resource.
     */
    recordingHeld(hash: string, resource: DataResource) {
        const key = hash.toUpperCase()
        this.#current.set(key, resource)
        for (const child of this.#children.get(key) ?? []) {
            if (takesParent(child)) {
                child.parent = resource
            }
        }
    }
}
