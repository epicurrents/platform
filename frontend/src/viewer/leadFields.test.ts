/**
 * Specs for the platform's lead-field provider.
 *
 * The module memoises its manifest fetch at module scope, so every case re-imports it after
 * `vi.resetModules()` rather than sharing one instance.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const MANIFEST_URL = '/vendor/leadfields/manifest.json'
const META_URL = '/compute/api/v1/eeg/leadfield/standard_1020/'

/** Load a fresh copy of the module, so the memoised manifest does not leak between cases. */
const loadProvider = async () => {
    vi.resetModules()
    return (await import('./leadFields')).leadFieldProvider
}

const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })

const notFound = () => new Response('', { status: 404 })

/** A manifest entry plus a blob sized to match it, so the static path can resolve. */
const staticBundle = () => {
    const nChannels = 2
    const nSources = 3
    const leadFieldBytes = nChannels*nSources*8
    const entry = {
        montage_name:       'standard_1020',
        n_orient:           1,
        grid_resolution_mm: 10,
        n_channels:         nChannels,
        n_sources:          nSources,
        channel_names:      ['Fp1', 'Fp2'],
        lead_field_bytes:   leadFieldBytes,
        src_pos_bytes:      nSources*3*8,
        file:               'standard_1020-abc123.bin',
        url:                'standard_1020-abc123.bin',
    }
    return {
        manifest: { entries: [entry], format_version: 1 },
        blob: new ArrayBuffer(leadFieldBytes + nSources*3*8),
    }
}

beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
})

describe('leadFieldProvider', () => {
    it('serves a montage from the static bundle without reaching the API', async () => {
        const { manifest, blob } = staticBundle()
        vi.mocked(fetch).mockImplementation(async (input) => {
            const url = String(input)
            if (url.includes(MANIFEST_URL)) {
                return jsonResponse(manifest)
            }
            if (url.includes('standard_1020-abc123.bin')) {
                return new Response(blob, { status: 200 })
            }
            throw new Error(`Unexpected fetch: ${url}`)
        })
        const provider = await loadProvider()
        const result = await provider('standard_1020', 1, 10)
        expect(result).not.toBeNull()
        expect(result?.channelNames).toEqual(['Fp1', 'Fp2'])
        expect(result?.nSources).toBe(3)
        // The API is the fallback, so a static hit must not have called it.
        const calls = vi.mocked(fetch).mock.calls.map((call) => String(call[0]))
        expect(calls.some((url) => url.includes('/compute/api/'))).toBe(false)
    })

    // 401 and 403 are the public viewer, which is auth-free and can never reach the compute API.
    // Treating them as failures would offer its users a retry that cannot succeed.
    it.each([401, 403, 404])('resolves null when the API answers %i', async (status) => {
        vi.mocked(fetch).mockImplementation(async (input) => {
            const url = String(input)
            if (url.includes(MANIFEST_URL)) {
                return notFound()
            }
            if (url.includes(META_URL)) {
                return new Response('', { status })
            }
            throw new Error(`Unexpected fetch: ${url}`)
        })
        const provider = await loadProvider()
        await expect(provider('standard_1020', 1, 10)).resolves.toBeNull()
    })

    it('rejects when the API fails for a reason the caller could retry', async () => {
        vi.mocked(fetch).mockImplementation(async (input) => {
            const url = String(input)
            if (url.includes(MANIFEST_URL)) {
                return notFound()
            }
            if (url.includes(META_URL)) {
                return new Response('', { status: 500 })
            }
            throw new Error(`Unexpected fetch: ${url}`)
        })
        const provider = await loadProvider()
        await expect(provider('standard_1020', 1, 10)).rejects.toThrow('HTTP 500')
    })
})
