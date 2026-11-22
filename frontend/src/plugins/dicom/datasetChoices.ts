/**
 * Which datasets the studies page offers a study to be added to.
 */

import type { Collection } from '#api/library'

/** A dataset a study can be added to, as the picker lists it. */
export interface DatasetChoice {
    /** The dataset's object hash, which names it in the items endpoint. */
    hash: string
    name: string
}

/**
 * The datasets among *datasets* the user can add a study to. Adding an item needs write access to the dataset, and
 * the dataset listing says only who the author is, so the choices are the user's own datasets, or every dataset for
 * a superuser, as the dataset page gates its own editing; a dataset the user holds a write grant on is not offered,
 * since the listing does not tell it from one they can only read. A release-gated dataset takes no study and is left
 * out, as is a row without a hash.
 * @param datasets - The datasets the user can read.
 * @param userId - The signed-in user's id, or null when there is none.
 * @param isSuperuser - The user is a superuser.
 */
export function datasetChoicesFor(
    datasets: Collection[],
    userId: number | null,
    isSuperuser: boolean,
): DatasetChoice[] {
    return datasets.flatMap(dataset => {
        const writable = isSuperuser || (userId !== null && dataset.author_id === userId)
        if (!writable || dataset.release_gated || !dataset.object_hash) {
            return []
        }
        return [{ hash: dataset.object_hash, name: dataset.name }]
    })
}
