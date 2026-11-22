/**
 * The manual order as the dataset page reads it off its item list, and how an order action's answer is put back.
 */

import { describe, expect, it } from 'vitest'
import { applyFolderOrder, isArranged, itemOrderStates, type OrderedItem } from './datasetItemOrder'

function item (id: number | null, position: number | null = null, folderId: number | null = null): OrderedItem {
    return { folder_id: folderId, id, position }
}

describe('isArranged', () => {
    it('is true for an item with a position and false for one without', () => {
        expect(isArranged(item(1, 3))).toBe(true)
        expect(isArranged(item(1))).toBe(false)
        expect(isArranged({ folder_id: null, id: 1 })).toBe(false)
    })
})

describe('itemOrderStates', () => {
    const items = [item(1, 1), item(2, 2), item(3, 5), item(4), item(5, 1, 7), item(6, null, 7)]
    const states = itemOrderStates(items)

    it('counts an arranged item among its folder\'s arranged rows, not by its stored position', () => {
        expect(states.get(items[2])).toEqual({ canMoveDown: false, canMoveUp: true, ordinal: 3 })
    })

    it('offers no move past either edge', () => {
        expect(states.get(items[0])).toEqual({ canMoveDown: true, canMoveUp: false, ordinal: 1 })
        expect(states.get(items[1])).toEqual({ canMoveDown: true, canMoveUp: true, ordinal: 2 })
    })

    it('keeps a folder\'s order to itself', () => {
        expect(states.get(items[4])).toEqual({ canMoveDown: false, canMoveUp: false, ordinal: 1 })
    })

    it('has no entry for an unsorted item', () => {
        expect(states.has(items[3])).toBe(false)
        expect(states.has(items[5])).toBe(false)
        expect(states.size).toBe(4)
    })
})

describe('applyFolderOrder', () => {
    it('puts the folder\'s members in the answered order with their new positions', () => {
        const items = [item(1, 1), item(2, 2), item(3)]
        const next = applyFolderOrder(items, null, [
            { id: 3, position: 1 },
            { id: 1, position: 2 },
            { id: 2, position: null },
        ])
        expect(next).toEqual([item(3, 1), item(1, 2), item(2)])
    })

    it('leaves the items of other folders where they were', () => {
        const items = [item(1), item(10, null, 7), item(2, 1), item(11, 1, 7)]
        const next = applyFolderOrder(items, 7, [{ id: 11, position: 1 }, { id: 10, position: 2 }])
        expect(next).toEqual([item(1), item(11, 1, 7), item(2, 1), item(10, 2, 7)])
        expect(next?.[0]).toBe(items[0])
    })

    it('does not change the list it was given', () => {
        const items = [item(1, 1), item(2)]
        applyFolderOrder(items, null, [{ id: 2, position: 1 }, { id: 1, position: 2 }])
        expect(items).toEqual([item(1, 1), item(2)])
    })

    it('answers null when the answer and the list disagree about the folder\'s members', () => {
        const items = [item(1, 1), item(2)]
        expect(applyFolderOrder(items, null, [{ id: 1, position: 1 }])).toBeNull()
        expect(applyFolderOrder(items, null, [{ id: 1, position: 1 }, { id: 9, position: 2 }])).toBeNull()
        expect(applyFolderOrder(items, null, [{ id: 1, position: 1 }, { id: 1, position: 2 }])).toBeNull()
    })

    it('answers null for a row named by no id, on either side', () => {
        expect(applyFolderOrder([item(1), item(null)], null, [{ id: 1, position: 1 }, { id: null, position: null }]))
            .toBeNull()
        expect(applyFolderOrder([item(1), item(2)], null, [{ id: 1, position: 1 }, { id: null, position: null }]))
            .toBeNull()
    })
})
