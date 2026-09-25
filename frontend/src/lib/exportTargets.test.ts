/**
 * Tests for the viewer export-target templates: how a batch's published profile becomes export constraints, and what
 * each target sends and reports back. The sidecar's declared hash is pinned against the bytes, since the submission
 * gate refuses a file whose declared hash differs from what arrived.
 */

import { createHash } from 'node:crypto'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#api/recordings', () => ({
    submitFile: vi.fn(),
    uploadRecording: vi.fn(),
}))
vi.mock('#i18n', () => ({
    t: (key: string, _scope: string, params: Record<string, unknown> = {}) =>
        key.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name])),
}))

import { submitFile, uploadRecording, type SubmissionBatch, type SubmissionProfile } from '#api/recordings'
import { createSubmissionTarget, createUploadTarget, profileConstraints } from './exportTargets'

const mockSubmit = vi.mocked(submitFile)
const mockUpload = vi.mocked(uploadRecording)

const batch: SubmissionBatch = {
    hash: '0123456789abcdef0123456789abcdef',
    dataset_hash: 'f'.repeat(32),
    profile: 'fictional.profile',
    pending_count: 0,
    failed_count: 0,
    ingested_count: 0,
    created_at: '2026-09-01T00:00:00Z',
}

function makeProfile(overrides: Partial<SubmissionProfile> = {}): SubmissionProfile {
    return {
        key: 'fictional.profile',
        channels: ['Fp1', 'Fp2'],
        sampling_rate: 128,
        physical_unit: 'uV',
        physical_min: -500,
        physical_max: 500,
        digital_min: -32768,
        digital_max: 32767,
        durations_seconds: [10, 20],
        required_sidecar_keys: ['recording_sha256'],
        forbidden_sidecar_keys: ['subject', 'text'],
        ...overrides,
    }
}

const bytes = new Uint8Array([1, 2, 3, 4]).buffer
const sha256 = createHash('sha256').update(new Uint8Array(bytes)).digest('hex')

/** The sidecar the last submission sent, parsed. */
async function sentSidecar(): Promise<Record<string, unknown>> {
    const blob = mockSubmit.mock.calls.at(-1)![2]
    return JSON.parse(await blob.text())
}

beforeEach(() => {
    mockSubmit.mockReset()
    mockUpload.mockReset()
})

describe('profileConstraints', () => {
    it('carries every value the gate checks', () => {
        expect(profileConstraints(makeProfile())).toEqual({
            amplitudeRange: [-500, 500],
            channels: ['Fp1', 'Fp2'],
            durations: [10, 20],
            forbiddenMetadataKeys: ['subject', 'text'],
            samplingRate: 128,
            unit: 'uV',
        })
    })

    it('leaves out what the profile does not check', () => {
        expect(profileConstraints(makeProfile({
            channels: [],
            sampling_rate: null,
            physical_unit: null,
            physical_min: -500,
            physical_max: null,
            digital_min: null,
            digital_max: null,
            durations_seconds: [],
            forbidden_sidecar_keys: [],
        }))).toEqual({})
    })

    it('refuses a digital range the encoder cannot write', () => {
        expect(profileConstraints(makeProfile({ digital_min: -2048, digital_max: 2047 }))).toBeNull()
        expect(profileConstraints(makeProfile({ digital_min: -32767 }))).toBeNull()
    })
})

describe('createUploadTarget', () => {
    it('sends the container to the upload and reports success', async () => {
        mockUpload.mockResolvedValue({} as never)
        const target = createUploadTarget()
        expect(target.options).toMatchObject({ deidentify: true, embedFooter: true })
        expect(target.sidecar).toBeFalsy()
        const result = await target.submit({ data: bytes, sidecar: null })
        expect(result.success).toBe(true)
        const sent = mockUpload.mock.calls[0][0]
        expect(new Uint8Array(await sent.arrayBuffer())).toEqual(new Uint8Array(bytes))
    })

    it('reports the refusal detail rather than throwing', async () => {
        mockUpload.mockRejectedValue({ response: { data: { detail: 'Quota exceeded.' } } })
        const result = await createUploadTarget().submit({ data: bytes, sidecar: null })
        expect(result).toEqual({ message: 'Quota exceeded.', success: false })
    })
})

describe('createSubmissionTarget', () => {
    it('is null for a profile the viewer cannot meet', () => {
        expect(createSubmissionTarget(batch, makeProfile({ digital_max: 2047 }))).toBeNull()
    })

    it('asks for a plain de-identified file and its de-identified sidecar', () => {
        const target = createSubmissionTarget(batch, makeProfile())!
        expect(target.format).toBe('edf')
        expect(target.sidecar).toBe(true)
        expect(target.options).toEqual({ deidentify: true, deidentifySidecar: true, embedFooter: false })
    })

    it('lets the host replace a constraint and the label', () => {
        const target = createSubmissionTarget(batch, makeProfile(), {
            constraints: { durations: [30] },
            label: 'Fictional pool',
        })!
        expect(target.label).toBe('Fictional pool')
        expect(target.constraints?.durations).toEqual([30])
        expect(target.constraints?.samplingRate).toBe(128)
    })

    it('declares the hash of the bytes it sends, after the host extends the sidecar', async () => {
        mockSubmit.mockResolvedValue({ accepted: true, pending_count: 3 })
        const target = createSubmissionTarget(batch, makeProfile(), {
            extendSidecar: (sidecar) => ({ ...sidecar, recording_sha256: 'forged', band: 'A2' }),
        })!
        const result = await target.submit({ data: bytes, sidecar: JSON.stringify({ version: '1.0' }) })
        expect(result).toEqual({ message: 'Accepted. Recordings waiting in the batch: 3.', success: true })
        expect(mockSubmit.mock.calls[0][0]).toBe(batch.hash)
        expect(await sentSidecar()).toEqual({ version: '1.0', band: 'A2', recording_sha256: sha256 })
    })

    it('sends nothing when the host declines to extend the sidecar, and reports its reason as written', async () => {
        const target = createSubmissionTarget(batch, makeProfile(), {
            extendSidecar: () => {
                throw new Error('The submission was cancelled.')
            },
        })!
        const result = await target.submit({ data: bytes, sidecar: '{}' })
        expect(result).toEqual({
            message: 'The sidecar could not be prepared: The submission was cancelled.',
            success: false,
        })
        expect(mockSubmit).not.toHaveBeenCalled()
    })

    it('refuses locally when the sidecar lacks a key the profile requires', async () => {
        const target = createSubmissionTarget(batch, makeProfile({ required_sidecar_keys: ['recording_sha256', 'band'] }))!
        const result = await target.submit({ data: bytes, sidecar: '{}' })
        expect(result.success).toBe(false)
        expect(result.message).toContain('band')
        expect(mockSubmit).not.toHaveBeenCalled()
    })

    it('reports the gate\'s violations', async () => {
        mockSubmit.mockResolvedValue({
            accepted: false,
            violations: [
                { code: 'duration', message: 'Wrong length.' },
                { code: 'channels', message: 'Wrong channels.' },
            ],
        })
        const result = await createSubmissionTarget(batch, makeProfile())!.submit({ data: bytes, sidecar: '{}' })
        expect(result).toEqual({ message: 'Refused: Wrong length. Wrong channels.', success: false })
    })

    it('reports a missing hash function rather than throwing', async () => {
        const digest = vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('insecure context'))
        const result = await createSubmissionTarget(batch, makeProfile())!.submit({ data: bytes, sidecar: '{}' })
        digest.mockRestore()
        expect(result.success).toBe(false)
        expect(mockSubmit).not.toHaveBeenCalled()
    })

    it('reports a sidecar that is not JSON without sending anything', async () => {
        const result = await createSubmissionTarget(batch, makeProfile())!.submit({ data: bytes, sidecar: '{' })
        expect(result.success).toBe(false)
        expect(mockSubmit).not.toHaveBeenCalled()
    })
})
