/**
 * The manual order of a dataset's items as the dataset page shows and changes it.
 *
 * A manager arranges the items of one folder, the dataset root being one: an arranged item carries a `position`, an
 * unsorted one none, and the server lists a folder's arranged items first. The page holds the items in the server's
 * order, so an item's place among the arranged ones is read off the list, and the answer to an order action, the
 * folder's members in their new order, is put back into the list in place.
 */

/** The fields of an item row the manual order is read from. */
export interface OrderedItem {
    id: number | null
    folder_id: number | null
    position?: number | null
}

/** Where an item stands in the manual order of its folder, and which order actions would change something. */
export interface ItemOrderState {
    /** The item's place among its folder's arranged items, from 1; null for an unsorted item. */
    ordinal: number | null
    /** An arranged item with one above it: moving up or to the top changes the order. */
    canMoveUp: boolean
    /** An arranged item with one below it: moving down or to the bottom changes the order. */
    canMoveDown: boolean
}

/** Whether an item has a place in the manual order of its folder. */
export function isArranged(item: OrderedItem): boolean {
    return item.position !== null && item.position !== undefined
}

/** The state of an item with no place in the manual order. */
export const UNSORTED: ItemOrderState = { canMoveDown: false, canMoveUp: false, ordinal: null }

/**
 * Where each arranged item stands among the arranged items of its folder; an unsorted item has no entry, and is
 * {@link UNSORTED}. The ordinal counts the rows, not the stored positions, which may skip a number after an item
 * was removed.
 * @param items - Every item of the dataset, in the server's order.
 */
export function itemOrderStates<T extends OrderedItem>(items: T[]): Map<T, ItemOrderState> {
    const byFolder = new Map<number | null, T[]>()
    for (const item of items) {
        if (isArranged(item)) {
            const arranged = byFolder.get(item.folder_id) ?? []
            arranged.push(item)
            byFolder.set(item.folder_id, arranged)
        }
    }
    const states = new Map<T, ItemOrderState>()
    for (const arranged of byFolder.values()) {
        arranged.forEach((item, index) => {
            states.set(item, { canMoveDown: index < arranged.length - 1, canMoveUp: index > 0, ordinal: index + 1 })
        })
    }
    return states
}

/**
 * The items after an order action: the members of the arranged folder in the order the server answered, each with
 * its new position, and every other item where it was. Null when the answer does not account for exactly the folder's
 * items as the page holds them (a member added or removed elsewhere, or a row the server names by no id), which the
 * caller answers by reading the listing again.
 * @param items - Every item of the dataset, in the order shown.
 * @param folderId - The folder the action arranged, null for the dataset root.
 * @param order - The folder's members after the action, in the server's order.
 */
export function applyFolderOrder<T extends OrderedItem>(
    items: T[],
    folderId: number | null,
    order: { id: number | null, position: number | null }[],
): T[] | null {
    const members = new Map<number, T>()
    let memberCount = 0
    for (const item of items) {
        if (item.folder_id !== folderId) {
            continue
        }
        memberCount++
        if (item.id !== null) {
            members.set(item.id, item)
        }
    }
    if (order.length !== memberCount || members.size !== memberCount) {
        return null
    }
    const placed: T[] = []
    for (const row of order) {
        const item = row.id === null ? undefined : members.get(row.id)
        if (!item) {
            return null
        }
        members.delete(row.id as number)
        placed.push({ ...item, position: row.position })
    }
    let next = 0
    return items.map(item => (item.folder_id === folderId ? placed[next++] : item))
}
