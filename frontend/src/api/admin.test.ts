/**
 * Regression tests for the group role write — the padded-map trap.
 *
 * `GroupDetailOut.roles` carries an entry for every registered role key,
 * including the ones the group holds nothing for. Seeding a form object from it
 * and submitting that back wholesale sends a map padded with nulls, and the
 * server reads an explicit null as "clear this role" — so the natural Vue idiom
 * silently clears every role the form did not render.
 *
 * These assert on the **request body**, not on the resulting state. A test that
 * checks the group's roles afterwards passes in the exact configuration where
 * the bug is dormant: while `/roles` and the group payload agree, a padded map
 * writes back the same values it read. It stops being harmless the moment the
 * `/roles` call fails or returns stale data — precisely when a silent clear is
 * least welcome.
 *
 * Role keys and values here are deliberately fictional, so a real one cannot
 * quietly become load-bearing in a fixture.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        patch: vi.fn(() => Promise.resolve({ data: {} })),
    },
    errorDetail: (_error: unknown, fallback: string) => fallback,
}))

import { rolesPayload, updateGroup, type GroupDetail } from '#api/admin'
import { http } from '#lib/http'

const mockPatch = vi.mocked(http.patch)

/** A group carrying one role, with a null padding entry for the other registered key. */
const GROUP: GroupDetail = {
    id: 7,
    name: 'Some group',
    member_count: 3,
    grant_count: 0,
    roles: { demo_widget_tier: 'tier_alpha', demo_colour: null },
}

beforeEach(() => {
    mockPatch.mockClear()
})

describe('rolesPayload', () => {
    it('carries only the keys the form rendered', () => {
        const payload = rolesPayload(['demo_widget_tier'], {
            demo_widget_tier: 'tier_beta',
            demo_colour: 'puce',
        })
        expect(Object.keys(payload)).toEqual(['demo_widget_tier'])
    })

    it('omits a registered key the form did not render, rather than padding it with null', () => {
        // The whole point: a client that has never heard of `demo_colour` must
        // not be able to clear it.
        const payload = rolesPayload(['demo_widget_tier'], GROUP.roles as Record<string, string>)
        expect(payload).not.toHaveProperty('demo_colour')
    })

    it('serialises "no role" as null, never an empty string', () => {
        // An empty string is not among a provider's declared choices, so the
        // server rejects it with a 400 rather than clearing the role.
        const payload = rolesPayload(['demo_colour'], { demo_colour: '' })
        expect(payload.demo_colour).toBeNull()
        expect(payload.demo_colour).not.toBe('')
    })

    it('treats a key with no entry in the form values as cleared', () => {
        const payload = rolesPayload(['demo_colour'], {})
        expect(payload).toEqual({ demo_colour: null })
    })
})

describe('updateGroup request body', () => {
    it('sends exactly the rendered keys', async () => {
        await updateGroup(GROUP.id, {
            name: 'Renamed',
            roles: rolesPayload(['demo_widget_tier'], { demo_widget_tier: 'tier_beta' }),
        })
        const [url, body] = mockPatch.mock.calls[0]
        expect(url).toBe('/api/v1/user/admin/groups/7')
        expect(body).toEqual({ name: 'Renamed', roles: { demo_widget_tier: 'tier_beta' } })
    })

    it('does not send a roles key at all when the deployment registers none', async () => {
        // The roleless deployment is the shape a dev stack with a project active
        // never shows by eye, so it is asserted rather than looked at.
        await updateGroup(GROUP.id, { name: 'Renamed' })
        const [, body] = mockPatch.mock.calls[0]
        expect(body).not.toHaveProperty('roles')
    })

    it('sends null for a role the operator cleared, and nothing for one not rendered', async () => {
        await updateGroup(GROUP.id, {
            roles: rolesPayload(['demo_widget_tier'], { demo_widget_tier: '' }),
        })
        const [, body] = mockPatch.mock.calls[0]
        expect(body).toEqual({ roles: { demo_widget_tier: null } })
    })
})
