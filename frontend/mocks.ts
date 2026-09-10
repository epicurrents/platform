/**
 * In-memory mock API server for Vite dev mode.
 *
 * Active when VITE_BACKEND_URL=mock. See vite.config.ts.
 * State resets to seed data on every full browser navigation so each
 * page reload starts from a known baseline.
 *
 * Covered endpoints:
 *   POST   /api/v1/user/login
 *   GET    /api/v1/user/me/2fa
 *   POST   /api/v1/user/logout
 *   GET    /api/v1/user/me
 *   PATCH  /api/v1/user/me
 *   POST   /api/v1/user/me/change-password
 *
 *   GET    /api/v1/user/admin/roles
 *   GET    /api/v1/user/admin/accounts
 *   POST   /api/v1/user/admin/accounts
 *   GET    /api/v1/user/admin/accounts/{id}
 *   PATCH  /api/v1/user/admin/accounts/{id}
 *   POST   /api/v1/user/admin/accounts/{id}/password
 *   DELETE /api/v1/user/admin/accounts/{id}/2fa
 *   PUT    /api/v1/user/admin/accounts/{id}/groups
 *   GET    /api/v1/user/admin/groups
 *   POST   /api/v1/user/admin/groups
 *   PATCH  /api/v1/user/admin/groups/{id}
 *   DELETE /api/v1/user/admin/groups/{id}
 *   PUT    /api/v1/user/admin/groups/{id}/members
 *
 *   GET    /annotations/api/v1/content-types
 *
 *   GET    /recordings/api/v1/
 *   POST   /recordings/api/v1/upload
 *   GET    /recordings/api/v1/status/{hash}
 *   GET    /recordings/api/v1/{hash}
 *   PATCH  /recordings/api/v1/{hash}
 *   DELETE /recordings/api/v1/{hash}
 *
 *   GET    /api/v1/library/collections/
 *   POST   /api/v1/library/collections/
 *   GET    /api/v1/library/collections/{id}/
 *   PATCH  /api/v1/library/collections/{id}/
 *   DELETE /api/v1/library/collections/{id}/
 *   GET    /api/v1/library/collections/{id}/items/
 *   POST   /api/v1/library/collections/{id}/items/
 *   DELETE /api/v1/library/collections/{id}/items/{itemId}/
 *
 *   GET    /api/v1/library/datasets/          (same shape as collections)
 *   POST   /api/v1/library/datasets/
 *   GET    /api/v1/library/datasets/{id}/
 *   PATCH  /api/v1/library/datasets/{id}/
 *   DELETE /api/v1/library/datasets/{id}/
 *   GET    /api/v1/library/datasets/{id}/items/
 *   POST   /api/v1/library/datasets/{id}/items/
 *   DELETE /api/v1/library/datasets/{id}/items/{itemId}/
 *   GET    /api/v1/library/datasets/{id}/access/
 *   POST   /api/v1/library/datasets/{id}/access/
 *   DELETE /api/v1/library/datasets/{id}/access/{rightId}/
 */

import { randomUUID } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'

// ─── Internal types ───────────────────────────────────────────────────────────

interface MockUser {
    id: number
    username: string
    email: string
    first_name: string
    last_name: string
    is_staff: boolean
    is_superuser: boolean
    is_2fa_enabled: boolean
}

interface MockRecording {
    _pk: number          // internal auto-increment PK — never sent to client
    hash: string         // 32-char lowercase hex
    original_name: string
    file_extension: string
    file_size: number
    file_hash: string
    content_hash: string
    status: 'pending' | 'processing' | 'ready' | 'failed'
    modality: string
    created_at: string
    deleted_at: string | null
    /** Author-set grantee-visible label; empty means the hash-prefix fallback is used. */
    custom_name?: string
    /** Author-only failure detail, set when status is 'failed'. */
    processing_error?: string
    meta: {
        format: string
        duration: number
        data_record_count: number
        data_record_duration: number
        signal_count: number
        discontinuous: boolean
    } | null
}

interface MockItem {
    id: number
    _parent_id: number      // which collection or dataset owns this item
    content_type_id: number
    object_id: string       // recording hash (for recordings; avoids PK exposure)
    added_at: string
    object_name: string | null
    object_hash: string | null
    deleted_at?: string | null   // soft-deleted with its collection (recursive trash)
}

interface MockAccess {
    id: number
    _parent_id: number      // which collection or dataset owns this right
    access_target_id: number | null
    access_target_group_id: number | null
    public_share_token: string | null
    can_read: boolean
    can_write: boolean
    can_share: boolean
    expires_at: string | null
}

interface MockGroup {
    id: number
    name: string
    description: string
    parent_id: number | null
    author_id: number
    created_at: string
    modified_at: string
    deleted_at: string | null
}

/** One row of the administration account roster. */
interface MockAccount extends MockUser {
    is_active: boolean
    date_joined: string
    last_login: string | null
    /** Password is never read back; held only so a set-password call has somewhere to land. */
    password: string
}

/** An auth group, with the roles it carries and the accounts in it. */
interface MockAuthGroup {
    id: number
    name: string
    roles: Record<string, string | null>
    memberIds: number[]
    /** Access grants targeting this group. Non-zero makes a delete return 409. */
    grantCount: number
}

interface MockState {
    user: MockUser
    accounts: MockAccount[]
    authGroups: MockAuthGroup[]
    recordings: MockRecording[]
    collections: MockGroup[]
    collectionItems: MockItem[]
    datasets: MockGroup[]
    datasetItems: MockItem[]
    datasetAccess: MockAccess[]
    seq: {
        rec: number
        coll: number
        ds: number
        item: number
        access: number
        account: number
        group: number
    }
}

// ─── Constants ────────────────────────────────────────────────────────────────

const RECORDING_CT_ID = 42
const MOCK_USER_ID = 1

/**
 * Role providers this mock deployment pretends its active project registers.
 *
 * The keys, values and labels are deliberately fictional: the platform never
 * knows a real role's meaning, and a real key sitting in a fixture is how one
 * quietly becomes load-bearing. Set this to `[]` to exercise the other
 * deployment shape — the roleless one, where no role UI may render at all,
 * which is the case a dev stack with a project active never shows by eye.
 */
const MOCK_ROLE_PROVIDERS = [
    {
        key: 'demo_widget_tier',
        label: 'Widget tier',
        choices: [['tier_alpha', 'Tier alpha'], ['tier_beta', 'Tier beta']],
    },
    {
        key: 'demo_colour',
        label: 'Demo colour',
        choices: [['puce', 'Puce'], ['chartreuse', 'Chartreuse']],
    },
]

/** Tracks which pending recordings have been polled once (to simulate processing). */
const _pendingFlipped = new Set<string>()

// ─── Session cookie ───────────────────────────────────────────────────────────

const SESSION_COOKIE = 'mock_session'
/**
 * Set-Cookie value that clears the mock session cookie.
 * Exported so vite.config.ts can set it on full-page navigations (which reset state).
 */
export const SESSION_COOKIE_CLEAR = `${SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Strict`

function isLoggedIn(req: IncomingMessage): boolean {
    const cookies = req.headers.cookie ?? ''
    return cookies.split(';').some(c => c.trim().startsWith(`${SESSION_COOKIE}=1`))
}

// ─── Seed data ────────────────────────────────────────────────────────────────

function ago(seconds: number): string {
    return new Date(Date.now() - seconds * 1000).toISOString()
}

function buildSeed(): MockState {
    const user: MockUser = {
        id: MOCK_USER_ID,
        username: 'mockuser',
        email: 'mock@epicurrents.dev',
        first_name: 'Mock',
        last_name: 'User',
        // Superuser so the staff- and superuser-gated surfaces (administration,
        // viewer settings, annotation export) are reachable in mock mode at all.
        is_staff: true,
        is_superuser: true,
        is_2fa_enabled: false,
    }

    const recordings: MockRecording[] = [
        {
            _pk: 1,
            hash: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
            original_name: 'sub-001_ses-baseline_task-rest_eeg.edf',
            custom_name: 'Baseline rest — P001',
            file_extension: '.edf',
            file_size: 12_345_678,
            file_hash: 'sha256:aabbccddeeff0011',
            content_hash: 'sha256:001122334455aabb',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 7),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 1800, data_record_count: 1800, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 2,
            hash: 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7',
            original_name: 'sub-002_ses-followup_task-rest_eeg.edf',
            file_extension: '.edf',
            file_size: 9_876_543,
            file_hash: 'sha256:112233445566aabb',
            content_hash: 'sha256:ccddee001122ff33',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 5),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 900, data_record_count: 900, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 3,
            hash: 'c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8',
            original_name: 'sub-003_emg_forearm_right.edf',
            file_extension: '.edf',
            file_size: 4_200_000,
            file_hash: 'sha256:556677889900aabb',
            content_hash: 'sha256:aabbcc001122dd33',
            status: 'ready',
            modality: 'emg',
            created_at: ago(3600 * 24 * 3),
            deleted_at: null,
            meta: { format: 'EDF', duration: 300, data_record_count: 300, data_record_duration: 1, signal_count: 8, discontinuous: false },
        },
        {
            _pk: 4,
            hash: 'd4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9',
            original_name: 'sub-004_ictal_ecog.edf',
            file_extension: '.edf',
            file_size: 33_000_000,
            file_hash: 'sha256:9900aabbccddeeff',
            content_hash: 'sha256:ffee001122334455',
            status: 'ready',
            modality: 'ecog',
            created_at: ago(3600 * 24 * 2),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 7200, data_record_count: 7200, data_record_duration: 1, signal_count: 128, discontinuous: false },
        },
        {
            _pk: 5,
            hash: 'e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0',
            original_name: 'sub-005_sleep_psg.edf',
            file_extension: '.edf',
            file_size: 8_000_000,
            file_hash: 'sha256:ddee001122ff3344',
            content_hash: 'sha256:cc8899aabbdd1122',
            status: 'pending',
            modality: 'eeg',
            created_at: ago(90),
            deleted_at: null,
            meta: null,
        },
        {
            _pk: 6,
            hash: 'f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1',
            original_name: 'sub-006_ses-baseline_task-p300_eeg.edf',
            file_extension: '.edf',
            file_size: 7_654_321,
            file_hash: 'sha256:aabb0011ccdd2233',
            content_hash: 'sha256:eeff4455aabb6677',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 14),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 2400, data_record_count: 2400, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 7,
            hash: 'a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2',
            original_name: 'sub-007_ses-baseline_task-erp_eeg.edf',
            file_extension: '.edf',
            file_size: 5_432_100,
            file_hash: 'sha256:8899aabbcc001122',
            content_hash: 'sha256:3344eeff00112233',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 12),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 1200, data_record_count: 1200, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 8,
            hash: 'b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3',
            original_name: 'sub-008_ses-followup_task-erp_eeg.edf',
            file_extension: '.edf',
            file_size: 6_100_000,
            file_hash: 'sha256:ccddeeff00112233',
            content_hash: 'sha256:44556677aabbccdd',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 11),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 1200, data_record_count: 1200, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 9,
            hash: 'c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4',
            original_name: 'sub-009_ses-baseline_task-rest_eeg.edf',
            file_extension: '.edf',
            file_size: 11_200_000,
            file_hash: 'sha256:eeff001122334455',
            content_hash: 'sha256:6677889900aabbcc',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 10),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 1800, data_record_count: 1800, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 10,
            hash: 'd0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5',
            original_name: 'sub-010_ses-baseline_task-rest_eeg.edf',
            file_extension: '.edf',
            file_size: 10_900_000,
            file_hash: 'sha256:1122334455667788',
            content_hash: 'sha256:99aabbccddeeff00',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 9),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 1800, data_record_count: 1800, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 11,
            hash: 'e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6',
            original_name: 'sub-011_ses-followup_task-rest_eeg.edf',
            file_extension: '.edf',
            file_size: 9_300_000,
            file_hash: 'sha256:aabbccdd00112233',
            content_hash: 'sha256:4455667788990011',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 8),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 900, data_record_count: 900, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 12,
            hash: 'f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7',
            original_name: 'sub-012_ses-followup_task-rest_eeg.edf',
            file_extension: '.edf',
            file_size: 8_750_000,
            file_hash: 'sha256:22334455aabbccdd',
            content_hash: 'sha256:eeff0011aabb2233',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 6),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 900, data_record_count: 900, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 13,
            hash: 'a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8',
            original_name: 'sub-013_ses-baseline_task-n-back_eeg.edf',
            file_extension: '.edf',
            file_size: 14_500_000,
            file_hash: 'sha256:6677889900aabbcc',
            content_hash: 'sha256:ddeeff0011223344',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 4),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 3600, data_record_count: 3600, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 14,
            hash: 'b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9',
            original_name: 'sub-014_ses-followup_task-n-back_eeg.edf',
            file_extension: '.edf',
            file_size: 13_800_000,
            file_hash: 'sha256:aabbccddeeff0011',
            content_hash: 'sha256:22334455667788aa',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 4),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 3600, data_record_count: 3600, data_record_duration: 1, signal_count: 64, discontinuous: false },
        },
        {
            _pk: 15,
            hash: 'c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0',
            original_name: 'sub-015_ses-baseline_task-ssvep_eeg.edf',
            file_extension: '.edf',
            file_size: 4_800_000,
            file_hash: 'sha256:bbc0d1e2f3a4b5c6',
            content_hash: 'sha256:d7e8f9a0b1c2d3e4',
            status: 'ready',
            modality: 'eeg',
            created_at: ago(3600 * 24 * 3),
            deleted_at: null,
            meta: { format: 'EDF+', duration: 600, data_record_count: 600, data_record_duration: 1, signal_count: 32, discontinuous: false },
        },
        {
            _pk: 16,
            hash: 'd6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1',
            original_name: 'sub-016_corrupt_headers_eeg.edf',
            file_extension: '.edf',
            file_size: 2_100_000,
            file_hash: 'sha256:ccd1e2f3a4b5c6d7',
            content_hash: 'sha256:e8f9a0b1c2d3e4f5',
            status: 'failed',
            modality: '',
            created_at: ago(3600 * 6),
            deleted_at: null,
            processing_error: 'Unsupported EDF header: data-record count field is not a valid integer.',
            meta: null,
        },
    ]

    const collections: MockGroup[] = [
        {
            id: 1,
            name: 'Sleep Studies',
            description: 'Overnight PSG and sleep-staged recordings',
            parent_id: null,
            author_id: MOCK_USER_ID,
            created_at: ago(3600 * 24 * 10),
            modified_at: ago(3600 * 24 * 5),
            deleted_at: null,
        },
        {
            id: 2,
            name: 'Epilepsy Cases',
            description: 'Ictal and inter-ictal recordings from epilepsy monitoring unit',
            parent_id: null,
            author_id: MOCK_USER_ID,
            created_at: ago(3600 * 24 * 8),
            modified_at: ago(3600 * 24 * 2),
            deleted_at: null,
        },
    ]

    const collectionItems: MockItem[] = [
        { id: 1, _parent_id: 1, content_type_id: RECORDING_CT_ID, object_id: recordings[0].hash, added_at: ago(3600 * 24 * 5), object_name: recordings[0].original_name, object_hash: recordings[0].hash },
        { id: 2, _parent_id: 1, content_type_id: RECORDING_CT_ID, object_id: recordings[1].hash, added_at: ago(3600 * 24 * 4), object_name: recordings[1].original_name, object_hash: recordings[1].hash },
        { id: 3, _parent_id: 2, content_type_id: RECORDING_CT_ID, object_id: recordings[3].hash, added_at: ago(3600 * 24 * 2), object_name: recordings[3].original_name, object_hash: recordings[3].hash },
    ]

    const datasets: MockGroup[] = [
        {
            id: 1,
            name: 'Public EEG Dataset',
            description: 'Openly shareable resting-state EEG recordings for the consortium',
            parent_id: null,
            author_id: MOCK_USER_ID,
            created_at: ago(3600 * 24 * 6),
            modified_at: ago(3600 * 24 * 6),
            deleted_at: null,
        },
        {
            id: 2,
            name: 'Research Cohort A',
            description: 'Longitudinal motor cortex recordings — internal use only',
            parent_id: null,
            author_id: MOCK_USER_ID,
            created_at: ago(3600 * 24 * 4),
            modified_at: ago(3600 * 24 * 4),
            deleted_at: null,
        },
    ]

    const datasetItems: MockItem[] = [
        { id: 1, _parent_id: 1, content_type_id: RECORDING_CT_ID, object_id: recordings[0].hash, added_at: ago(3600 * 24 * 6), object_name: recordings[0].original_name, object_hash: recordings[0].hash },
        { id: 2, _parent_id: 1, content_type_id: RECORDING_CT_ID, object_id: recordings[1].hash, added_at: ago(3600 * 24 * 6), object_name: recordings[1].original_name, object_hash: recordings[1].hash },
        { id: 3, _parent_id: 2, content_type_id: RECORDING_CT_ID, object_id: recordings[2].hash, added_at: ago(3600 * 24 * 4), object_name: recordings[2].original_name, object_hash: recordings[2].hash },
    ]

    const datasetAccess: MockAccess[] = [
        { id: 1, _parent_id: 1, access_target_id: null, access_target_group_id: null, public_share_token: 'xyz-public-eeg-dataset', can_read: true, can_write: false, can_share: false, expires_at: null },
    ]

    const accounts: MockAccount[] = [
        {
            ...user,
            is_active: true,
            date_joined: ago(3600 * 24 * 400),
            last_login: ago(3600),
            password: 'mock',
        },
        {
            id: 2,
            username: 'rkeller',
            email: 'r.keller@epicurrents.dev',
            first_name: 'Robin',
            last_name: 'Keller',
            is_staff: true,
            is_superuser: false,
            is_2fa_enabled: true,
            is_active: true,
            date_joined: ago(3600 * 24 * 200),
            last_login: ago(3600 * 24 * 2),
            password: 'mock',
        },
        {
            id: 3,
            username: 'jmoreau',
            email: 'j.moreau@epicurrents.dev',
            first_name: 'Jules',
            last_name: 'Moreau',
            is_staff: false,
            is_superuser: false,
            is_2fa_enabled: false,
            is_active: true,
            date_joined: ago(3600 * 24 * 30),
            last_login: null,
            password: 'mock',
        },
        {
            // Deactivated and unnamed, so the roster shows both the inactive
            // marker and the username fallback for a row with no display name.
            id: 4,
            username: 'former.account',
            email: 'former@epicurrents.dev',
            first_name: '',
            last_name: '',
            is_staff: false,
            is_superuser: false,
            is_2fa_enabled: false,
            is_active: false,
            date_joined: ago(3600 * 24 * 900),
            last_login: ago(3600 * 24 * 500),
            password: 'mock',
        },
    ]

    const authGroups: MockAuthGroup[] = [
        {
            id: 1,
            name: 'Reviewers',
            roles: { demo_widget_tier: 'tier_alpha', demo_colour: null },
            memberIds: [1, 2],
            // Non-zero, so deleting this group is refused the way the real API refuses it.
            grantCount: 2,
        },
        {
            id: 2,
            name: 'Trainees',
            roles: { demo_widget_tier: null, demo_colour: 'chartreuse' },
            memberIds: [3],
            grantCount: 0,
        },
    ]

    return {
        user,
        accounts,
        authGroups,
        recordings,
        collections,
        collectionItems,
        datasets,
        datasetItems,
        datasetAccess,
        seq: { rec: 17, coll: 3, ds: 3, item: 4, access: 2, account: 5, group: 3 },
    }
}

let _state: MockState

export function resetState(): void {
    _state = buildSeed()
    _pendingFlipped.clear()
}

// Initialize on module load so the first request is always ready.
resetState()

// ─── HTTP helpers ─────────────────────────────────────────────────────────────

function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
        const chunks: Buffer[] = []
        req.on('data', (chunk: Buffer) => chunks.push(chunk))
        req.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf-8')
            try { resolve(raw ? (JSON.parse(raw) as Record<string, unknown>) : {}) }
            catch { resolve({}) }
        })
        req.on('error', reject)
    })
}

function send(res: ServerResponse, status: number, data: unknown): true {
    const body = JSON.stringify(data)
    res.writeHead(status, {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
    })
    res.end(body)
    return true
}

function noContent(res: ServerResponse): true {
    res.writeHead(204)
    res.end()
    return true
}

function notFound(res: ServerResponse): true {
    return send(res, 404, { detail: 'Not found.' })
}

function conflict(res: ServerResponse, detail: string): true {
    return send(res, 409, { detail })
}

// ─── Serialisation helpers ────────────────────────────────────────────────────

/** Groups an account belongs to, as the reference shape the account payload carries. */
function groupsOf(accountId: number) {
    return _state.authGroups
        .filter(group => group.memberIds.includes(accountId))
        .map(group => ({ id: group.id, name: group.name }))
}

/**
 * One account as `AccountOut`, with roles gathered across every group it is in.
 *
 * The value is a list per key because a user inherits a role from each group
 * carrying it, and several groups may carry the same one — the group form is
 * single-select, the account display is not.
 */
function accountOut(account: MockAccount) {
    const roles: Record<string, string[]> = {}
    for (const group of _state.authGroups) {
        if (!group.memberIds.includes(account.id)) continue
        for (const [key, value] of Object.entries(group.roles)) {
            if (value === null) continue
            roles[key] = [...new Set([...(roles[key] ?? []), value])]
        }
    }
    return {
        id: account.id,
        username: account.username,
        email: account.email,
        first_name: account.first_name,
        last_name: account.last_name,
        is_active: account.is_active,
        is_staff: account.is_staff,
        is_superuser: account.is_superuser,
        is_2fa_enabled: account.is_2fa_enabled,
        date_joined: account.date_joined,
        last_login: account.last_login,
        groups: groupsOf(account.id),
        roles,
    }
}

/** One group as `GroupDetailOut`, with the two counts that decide deletability. */
function groupOut(group: MockAuthGroup) {
    return {
        id: group.id,
        name: group.name,
        member_count: group.memberIds.length,
        grant_count: group.grantCount,
        roles: { ...group.roles },
    }
}

/**
 * The refusal message when a change would leave no active superuser, or `null`
 * when it would not. Mirrors the server guard, which is the only thing standing
 * between an operator and locking everyone out of account administration.
 */
function lastSuperuserRefusal(account: MockAccount, nextActive: boolean, nextSuperuser: boolean): string | null {
    if (!account.is_superuser || !account.is_active) return null
    if (nextActive && nextSuperuser) return null
    const remaining = _state.accounts.filter(a => a.is_superuser && a.is_active && a.id !== account.id).length
    if (remaining > 0) return null
    return 'This is the last active superuser. Promote another account first, or the deployment loses '
        + 'access to account administration entirely.'
}

/** Strip internal _parent_id before sending an item to the client. */
function itemOut(item: MockItem) {
    const { _parent_id: _, ...rest } = item
    return rest
}

/** Strip internal _parent_id before sending an access right to the client. */
function accessOut(access: MockAccess) {
    const { _parent_id: _, ...rest } = access
    return rest
}

/** The grantee-visible label: the custom name if set, else the hash-prefix fallback. */
function resolvedDisplayName(r: MockRecording): string {
    const custom = (r.custom_name ?? '').trim()
    return custom || r.hash.slice(0, 8).toUpperCase()
}

/** Build the public Recording response shape (no integer PK). The mock caller is
 *  always the author, so author-only fields (original_name, processing_error) are
 *  returned unconditionally. */
function recordingOut(r: MockRecording) {
    return {
        hash: r.hash,
        original_name: r.original_name,
        display_name: resolvedDisplayName(r),
        has_custom_name: !!(r.custom_name ?? '').trim(),
        processing_error: r.processing_error || null,
        file_extension: r.file_extension,
        file_size: r.file_size,
        file_hash: r.file_hash,
        content_hash: r.content_hash,
        status: r.status,
        modality: r.modality,
        created_at: r.created_at,
        deleted_at: r.deleted_at,
        meta: r.meta,
        events: [],
        interruptions: [],
        labels: [],
        trashed_collection: trashedCollectionCue(r.hash),
    }
}

/** Cue for a root-surfaced recording: the trashed collection it will return to,
 *  or null when it has a live membership or is genuinely uncollected. */
function trashedCollectionCue(hash: string): { id: number; name: string } | null {
    const hasLive = _state.collectionItems.some(
        i => i.content_type_id === RECORDING_CT_ID && i.object_hash === hash && !i.deleted_at,
    )
    if (hasLive) {
        return null
    }
    const trashedMembership = _state.collectionItems.find(
        i => i.content_type_id === RECORDING_CT_ID && i.object_hash === hash && i.deleted_at,
    )
    if (!trashedMembership) {
        return null
    }
    const parent = _state.collections.find(c => c.id === trashedMembership._parent_id && c.deleted_at)
    return parent ? { id: parent.id, name: parent.name } : null
}

/** Ids of a collection and every collection beneath it in the tree. */
function subtreeCollectionIds(rootId: number): Set<number> {
    const ids = new Set([rootId])
    let frontier = [rootId]
    while (frontier.length) {
        const children = _state.collections
            .filter(c => c.parent_id !== null && frontier.includes(c.parent_id))
            .map(c => c.id)
        frontier = children.filter(id => !ids.has(id))
        frontier.forEach(id => ids.add(id))
    }
    return ids
}

// ─── Domain helpers ───────────────────────────────────────────────────────────

function findRecording(hashOrPk: string): MockRecording | undefined {
    const pk = Number(hashOrPk)
    return _state.recordings.find(r =>
        !r.deleted_at && (r.hash === hashOrPk || (!isNaN(pk) && r._pk === pk)),
    )
}

/**
 * Resolve display metadata for a collection / dataset item.
 * For recordings the object_id is stored as the hash itself (no integer PK exposure).
 */
function enrichItem(contentTypeId: number, objectId: string): { object_name: string | null; object_hash: string | null } {
    if (contentTypeId === RECORDING_CT_ID) {
        const rec = findRecording(objectId)
        if (rec) return { object_name: resolvedDisplayName(rec), object_hash: rec.hash }
    }
    return { object_name: null, object_hash: null }
}

// ─── Route handler ────────────────────────────────────────────────────────────

export async function handleMock(
    method: string,
    path: string,
    req: IncomingMessage,
    res: ServerResponse,
): Promise<boolean> {

    // ── User API ──────────────────────────────────────────────────────────────

    if (path === '/api/v1/user/login' && method === 'POST') {
        res.setHeader('Set-Cookie', `${SESSION_COOKIE}=1; Path=/; HttpOnly; SameSite=Strict`)
        // The mock account has no second factor, so login is always one step.
        // The envelope is still the real shape — a client that reads the user
        // off the top level would work here and break against the backend.
        return send(res, 200, { authenticated: true, two_factor_required: false, user: _state.user })
    }

    if (path === '/api/v1/user/logout' && method === 'POST') {
        res.setHeader('Set-Cookie', SESSION_COOKIE_CLEAR)
        return noContent(res)
    }

    if (path === '/api/v1/user/me/2fa' && method === 'GET') {
        if (!isLoggedIn(req)) return send(res, 401, { detail: 'Authentication credentials were not provided.' })
        return send(res, 200, { enabled: false, confirmed_at: null, backup_codes_remaining: 0 })
    }

    if (path === '/api/v1/user/me') {
        if (!isLoggedIn(req)) return send(res, 401, { detail: 'Authentication credentials were not provided.' })
        // GET answers with the auth-state envelope, matching the backend: the
        // SPA reads `data.user`, so returning the bare user here would leave the
        // store holding undefined and only show up as odd behaviour downstream.
        if (method === 'GET') return send(res, 200, { authenticated: true, user: _state.user })
        if (method === 'PATCH') {
            const body = await readBody(req)
            if (typeof body.email === 'string') _state.user.email = body.email
            if (typeof body.first_name === 'string') _state.user.first_name = body.first_name
            if (typeof body.last_name === 'string') _state.user.last_name = body.last_name
            return send(res, 200, _state.user)
        }
    }

    if (path === '/api/v1/user/me/change-password' && method === 'POST') {
        if (!isLoggedIn(req)) return send(res, 401, { detail: 'Authentication credentials were not provided.' })
        return noContent(res)
    }

    // ── Account administration ────────────────────────────────────────────────

    if (path.startsWith('/api/v1/user/admin/')) {
        if (!isLoggedIn(req)) return send(res, 401, { detail: 'Authentication credentials were not provided.' })
        if (!_state.user.is_staff && !_state.user.is_superuser) {
            return send(res, 403, { detail: 'This requires staff access.' })
        }
        // Every write below is superuser-only, matching the real tier split.
        const isWrite = method !== 'GET'
        if (isWrite && !_state.user.is_superuser) {
            return send(res, 403, { detail: 'This requires superuser access.' })
        }
        const tail = path.slice('/api/v1/user/admin/'.length)

        if (tail === 'roles' && method === 'GET') {
            return send(res, 200, MOCK_ROLE_PROVIDERS)
        }

        if (tail === 'accounts' && method === 'GET') {
            const url = new URL(path + (req.url?.includes('?') ? req.url.slice(req.url.indexOf('?')) : ''), 'http://mock')
            const q = (url.searchParams.get('q') ?? '').toLowerCase()
            const limit = Number(url.searchParams.get('limit') ?? 100)
            const offset = Number(url.searchParams.get('offset') ?? 0)
            const matched = _state.accounts.filter(account => {
                if (!q) return true
                return [account.username, account.first_name, account.last_name, account.email]
                    .some(field => field.toLowerCase().includes(q))
            })
            return send(res, 200, matched.slice(offset, offset + limit).map(accountOut))
        }

        if (tail === 'accounts' && method === 'POST') {
            const body = await readBody(req)
            const username = String(body.username ?? '').trim()
            if (!username) return send(res, 400, { detail: 'Username is required.' })
            if (_state.accounts.some(a => a.username.toLowerCase() === username.toLowerCase())) {
                return conflict(res, 'An account with that username already exists.')
            }
            const password = String(body.password ?? '')
            if (password.length < 8) {
                return send(res, 400, { detail: 'This password is too short. It must contain at least 8 characters.' })
            }
            const account: MockAccount = {
                id: _state.seq.account++,
                username,
                email: String(body.email ?? ''),
                first_name: String(body.first_name ?? ''),
                last_name: String(body.last_name ?? ''),
                is_active: body.is_active !== false,
                is_staff: body.is_staff === true,
                is_superuser: body.is_superuser === true,
                is_2fa_enabled: false,
                date_joined: new Date().toISOString(),
                last_login: null,
                password,
            }
            _state.accounts.push(account)
            return send(res, 201, accountOut(account))
        }

        const accountMatch = /^accounts\/(\d+)(\/groups|\/password|\/2fa)?$/.exec(tail)
        if (accountMatch) {
            const account = _state.accounts.find(a => a.id === Number(accountMatch[1]))
            if (!account) return send(res, 404, { detail: 'Account not found.' })
            const suffix = accountMatch[2] ?? ''

            if (suffix === '' && method === 'GET') {
                return send(res, 200, accountOut(account))
            }

            if (suffix === '' && method === 'PATCH') {
                const body = await readBody(req)
                const nextActive = body.is_active === undefined ? account.is_active : body.is_active === true
                const nextSuper = body.is_superuser === undefined ? account.is_superuser : body.is_superuser === true
                const guard = lastSuperuserRefusal(account, nextActive, nextSuper)
                if (guard) return conflict(res, guard)
                if (body.email !== undefined) account.email = String(body.email)
                if (body.first_name !== undefined) account.first_name = String(body.first_name)
                if (body.last_name !== undefined) account.last_name = String(body.last_name)
                if (body.is_staff !== undefined) account.is_staff = body.is_staff === true
                account.is_active = nextActive
                account.is_superuser = nextSuper
                if (account.id === MOCK_USER_ID) {
                    _state.user.is_staff = account.is_staff
                    _state.user.is_superuser = account.is_superuser
                }
                return send(res, 200, accountOut(account))
            }

            if (suffix === '/password' && method === 'POST') {
                const body = await readBody(req)
                const password = String(body.new_password ?? '')
                if (password.length < 8) {
                    return send(res, 400, {
                        detail: 'This password is too short. It must contain at least 8 characters.',
                    })
                }
                account.password = password
                return send(res, 200, { status: 'ok' })
            }

            if (suffix === '/2fa' && method === 'DELETE') {
                account.is_2fa_enabled = false
                if (account.id === MOCK_USER_ID) _state.user.is_2fa_enabled = false
                return send(res, 200, { status: 'reset' })
            }

            if (suffix === '/groups' && method === 'PUT') {
                const body = await readBody(req)
                const groupIds = Array.isArray(body.group_ids) ? (body.group_ids as number[]).map(Number) : null
                if (groupIds === null) return send(res, 400, { detail: 'group_ids is required.' })
                const missing = groupIds.filter(id => !_state.authGroups.some(g => g.id === id))
                if (missing.length) return send(res, 404, { detail: `No such group: ${missing.join(', ')}.` })
                for (const group of _state.authGroups) {
                    const shouldHold = groupIds.includes(group.id)
                    group.memberIds = group.memberIds.filter(id => id !== account.id)
                    if (shouldHold) group.memberIds.push(account.id)
                }
                return send(res, 200, accountOut(account))
            }
        }

        if (tail === 'groups' && method === 'GET') {
            return send(res, 200, _state.authGroups.map(groupOut))
        }

        if (tail === 'groups' && method === 'POST') {
            const body = await readBody(req)
            const name = String(body.name ?? '').trim()
            if (!name) return send(res, 400, { detail: 'Group name is required.' })
            if (_state.authGroups.some(g => g.name.toLowerCase() === name.toLowerCase())) {
                return conflict(res, 'A group with that name already exists.')
            }
            const group: MockAuthGroup = {
                id: _state.seq.group++,
                name,
                // Every registered key present with a null, mirroring the real payload —
                // which is exactly the shape a client must not send straight back.
                roles: Object.fromEntries(MOCK_ROLE_PROVIDERS.map(p => [p.key, null])),
                memberIds: [],
                grantCount: 0,
            }
            _state.authGroups.push(group)
            return send(res, 201, groupOut(group))
        }

        const groupMatch = /^groups\/(\d+)(\/members)?$/.exec(tail)
        if (groupMatch) {
            const group = _state.authGroups.find(g => g.id === Number(groupMatch[1]))
            if (!group) return send(res, 404, { detail: 'Group not found.' })
            const suffix = groupMatch[2] ?? ''

            if (suffix === '' && method === 'PATCH') {
                const body = await readBody(req)
                const roles = body.roles as Record<string, string | null> | undefined
                // Validated before anything is written: the real endpoint writes
                // name and roles in one transaction, so a bad role value must
                // abort the rename with it rather than half-succeed.
                if (roles) {
                    for (const [key, value] of Object.entries(roles)) {
                        const provider = MOCK_ROLE_PROVIDERS.find(p => p.key === key)
                        if (!provider) return send(res, 400, { detail: `Unknown role: ${key}.` })
                        if (value !== null && !provider.choices.some(choice => choice[0] === value)) {
                            return send(res, 400, { detail: `"${value}" is not a valid value for ${key}.` })
                        }
                    }
                }
                if (body.name !== undefined) {
                    const name = String(body.name).trim()
                    if (!name) return send(res, 400, { detail: 'Group name is required.' })
                    group.name = name
                }
                // Absent keys are left alone; an explicit null clears that role.
                if (roles) Object.assign(group.roles, roles)
                return send(res, 200, groupOut(group))
            }

            if (suffix === '' && method === 'DELETE') {
                if (group.grantCount) {
                    return conflict(
                        res,
                        `${group.grantCount} access grant(s) still target this group. Revoke them first — deleting `
                        + 'the group would remove them silently, leaving no record of what access was withdrawn.',
                    )
                }
                _state.authGroups = _state.authGroups.filter(g => g.id !== group.id)
                return send(res, 200, { status: 'deleted' })
            }

            if (suffix === '/members' && method === 'PUT') {
                const body = await readBody(req)
                const userIds = Array.isArray(body.user_ids) ? (body.user_ids as number[]).map(Number) : null
                if (userIds === null) return send(res, 400, { detail: 'user_ids is required.' })
                const missing = userIds.filter(id => !_state.accounts.some(a => a.id === id))
                if (missing.length) return send(res, 404, { detail: `No such account: ${missing.join(', ')}.` })
                group.memberIds = [...userIds]
                return send(res, 200, groupOut(group))
            }
        }

        return notFound(res)
    }

    // ── Annotation export ─────────────────────────────────────────────────────

    if (path === '/annotations/api/v1/export/annotators' && method === 'GET') {
        if (!isLoggedIn(req)) return send(res, 401, { detail: 'Authentication credentials were not provided.' })
        if (!_state.user.is_superuser) return send(res, 403, { detail: 'Listing annotators requires staff access.' })
        return send(res, 200, {
            annotators: [
                {
                    id: _state.user.id,
                    username: _state.user.username,
                    name: `${_state.user.first_name} ${_state.user.last_name}`.trim() || _state.user.username,
                    events: 3,
                    labels: 1,
                },
            ],
        })
    }

    // ── Content types ─────────────────────────────────────────────────────────

    if (path === '/annotations/api/v1/content-types' && method === 'GET') {
        return send(res, 200, [
            { id: RECORDING_CT_ID, app_label: 'recordings', model: 'recording', natural_key: 'recordings.recording' },
        ])
    }

    // ── Recordings API ────────────────────────────────────────────────────────

    if (path === '/recordings/api/v1/' && method === 'GET') {
        const params = new URLSearchParams((req.url ?? '').split('?')[1] ?? '')
        const statusFilter = params.get('status')
        const uncollected = params.get('uncollected') === 'true'
        const limit = Number(params.get('limit') ?? '50')
        const offset = Number(params.get('offset') ?? '0')
        let recs = _state.recordings.filter(r => !r.deleted_at)
        if (statusFilter) {
            recs = recs.filter(r => r.status === statusFilter)
        }
        if (uncollected) {
            // Root surfacing excludes only recordings with a *live* membership;
            // a trashed-collection membership no longer files it anywhere.
            const liveCollected = new Set(
                _state.collectionItems
                    .filter(i => i.content_type_id === RECORDING_CT_ID && !i.deleted_at)
                    .map(i => i.object_hash),
            )
            recs = recs.filter(r => !liveCollected.has(r.hash))
        }
        recs.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        return send(res, 200, recs.slice(offset, offset + limit).map(recordingOut))
    }

    if (path === '/recordings/api/v1/upload' && method === 'POST') {
        const hash = randomUUID().replace(/-/g, '')
        const pk = _state.seq.rec++
        const rec: MockRecording = {
            _pk: pk,
            hash,
            original_name: 'uploaded_recording.edf',
            file_extension: '.edf',
            file_size: 1_000_000,
            file_hash: `sha256:mock${pk}`,
            content_hash: `sha256:cnt${pk}`,
            status: 'pending',
            modality: '',
            created_at: new Date().toISOString(),
            deleted_at: null,
            meta: null,
        }
        _state.recordings.push(rec)
        // Return RecordingUpload shape (id is the internal pk — only used transiently)
        return send(res, 201, {
            id: pk,
            original_name: rec.original_name,
            stored_name: `${hash.toUpperCase()}.edf`,
            file_extension: rec.file_extension,
            file_size: rec.file_size,
            file_hash: rec.file_hash,
            status: rec.status,
        })
    }

    // GET /recordings/api/v1/status/{hash}
    {
        const m = path.match(/^\/recordings\/api\/v1\/status\/([a-fA-F0-9]{32})$/)
        if (m && method === 'GET') {
            const hash = m[1].toLowerCase()
            const rec = findRecording(hash)
            if (!rec) return notFound(res)
            // Simulate async processing: first poll → still pending; second poll → ready.
            if (rec.status === 'pending') {
                if (!_pendingFlipped.has(hash)) {
                    _pendingFlipped.add(hash)
                } else {
                    rec.status = 'ready'
                    rec.meta = { format: 'EDF+', duration: 600, data_record_count: 600, data_record_duration: 1, signal_count: 16, discontinuous: false }
                }
            }
            return send(res, 200, { id: rec._pk, status: rec.status })
        }
    }

    // GET|PATCH|DELETE /recordings/api/v1/{hash} — metadata; bytes live at /{hash}/file,
    // which the mock does not serve.
    {
        const m = path.match(/^\/recordings\/api\/v1\/([a-fA-F0-9]{32})$/)
        if (m) {
            const hash = m[1].toLowerCase()
            const rec = findRecording(hash)
            if (!rec) return notFound(res)
            if (method === 'GET') return send(res, 200, recordingOut(rec))
            if (method === 'PATCH') {
                const body = await readBody(req)
                if (typeof body.display_name === 'string') {
                    // Empty string clears the custom label and reverts to the hash prefix.
                    rec.custom_name = body.display_name.trim()
                    // Keep enriched names in collection/dataset items in sync.
                    const resolved = resolvedDisplayName(rec)
                    for (const item of [..._state.collectionItems, ..._state.datasetItems]) {
                        if (item.object_hash === hash) item.object_name = resolved
                    }
                }
                if (typeof body.modality === 'string') rec.modality = body.modality.trim().toLowerCase()
                return send(res, 200, recordingOut(rec))
            }
            if (method === 'DELETE') {
                rec.deleted_at = new Date().toISOString()
                return noContent(res)
            }
        }
    }

    // ── Collections ───────────────────────────────────────────────────────────

    if (path === '/api/v1/library/collections/') {
        if (method === 'GET') {
            return send(res, 200, _state.collections.filter(c => !c.deleted_at))
        }
        if (method === 'POST') {
            const body = await readBody(req)
            const coll: MockGroup = {
                id: _state.seq.coll++,
                name: String(body.name ?? 'Untitled'),
                description: String(body.description ?? ''),
                parent_id: (body.parent_id as number | null) ?? null,
                author_id: MOCK_USER_ID,
                created_at: new Date().toISOString(),
                modified_at: new Date().toISOString(),
                deleted_at: null,
            }
            _state.collections.push(coll)
            return send(res, 201, coll)
        }
    }

    // GET|PATCH|DELETE /api/v1/library/collections/{id}/
    {
        const m = path.match(/^\/api\/v1\/library\/collections\/(\d+)\/$/)
        if (m) {
            const id = Number(m[1])
            const coll = _state.collections.find(c => c.id === id && !c.deleted_at)
            if (!coll) return notFound(res)
            if (method === 'GET') return send(res, 200, coll)
            if (method === 'PATCH') {
                const body = await readBody(req)
                if (typeof body.name === 'string') coll.name = body.name
                if (typeof body.description === 'string') coll.description = body.description
                if ('parent_id' in body) coll.parent_id = (body.parent_id as number | null) ?? null
                coll.modified_at = new Date().toISOString()
                return send(res, 200, coll)
            }
            if (method === 'DELETE') {
                // Recursive trash: the whole subtree (sub-collections + memberships)
                // shares one timestamp so restore can lift exactly these rows.
                const now = new Date().toISOString()
                const subtree = subtreeCollectionIds(coll.id)
                for (const c of _state.collections) {
                    if (subtree.has(c.id) && !c.deleted_at) {
                        c.deleted_at = now
                    }
                }
                for (const it of _state.collectionItems) {
                    if (subtree.has(it._parent_id) && !it.deleted_at) {
                        it.deleted_at = now
                    }
                }
                return noContent(res)
            }
        }
    }

    // POST /api/v1/library/collections/{id}/restore
    {
        const m = path.match(/^\/api\/v1\/library\/collections\/(\d+)\/restore$/)
        if (m && method === 'POST') {
            const coll = _state.collections.find(c => c.id === Number(m[1]) && c.deleted_at)
            if (!coll) return notFound(res)
            const trashedAt = coll.deleted_at
            const subtree = subtreeCollectionIds(coll.id)
            for (const c of _state.collections) {
                if (subtree.has(c.id) && c.deleted_at === trashedAt) {
                    c.deleted_at = null
                }
            }
            let itemsRestored = 0
            let itemsSkipped = 0
            for (const it of _state.collectionItems) {
                if (!(subtree.has(it._parent_id) && it.deleted_at === trashedAt)) {
                    continue
                }
                // Re-filing wins: leave trashed if the object is now filed live.
                const conflict = _state.collectionItems.some(
                    o => o.content_type_id === it.content_type_id
                        && o.object_id === it.object_id
                        && !o.deleted_at,
                )
                if (conflict) {
                    itemsSkipped++
                    continue
                }
                it.deleted_at = null
                itemsRestored++
            }
            return send(res, 200, {
                status: 'ok',
                items_restored: itemsRestored,
                items_skipped: itemsSkipped,
            })
        }
    }

    // GET|POST /api/v1/library/collections/{id}/items/
    {
        const m = path.match(/^\/api\/v1\/library\/collections\/(\d+)\/items\/$/)
        if (m) {
            const collId = Number(m[1])
            if (method === 'GET') {
                return send(res, 200, _state.collectionItems.filter(i => i._parent_id === collId && !i.deleted_at).map(itemOut))
            }
            if (method === 'POST') {
                const body = await readBody(req)
                const ctId = Number(body.content_type_id)
                const objectId = String(body.object_id ?? '').trim()
                if (_state.collectionItems.some(i => i._parent_id === collId && i.object_id === objectId && i.content_type_id === ctId && !i.deleted_at)) {
                    return conflict(res, 'Item already in collection.')
                }
                const item: MockItem = {
                    id: _state.seq.item++,
                    _parent_id: collId,
                    content_type_id: ctId,
                    object_id: objectId,
                    added_at: new Date().toISOString(),
                    ...enrichItem(ctId, objectId),
                }
                _state.collectionItems.push(item)
                return send(res, 201, itemOut(item))
            }
        }
    }

    // POST /api/v1/library/collections/{id}/recordings/bulk-rename
    {
        const m = path.match(/^\/api\/v1\/library\/collections\/(\d+)\/recordings\/bulk-rename$/)
        if (m && method === 'POST') {
            const collId = Number(m[1])
            const body = await readBody(req)
            const prefix = (typeof body.prefix === 'string' && body.prefix.trim()) || 'Recording'
            const items = _state.collectionItems
                .filter(i => i._parent_id === collId && i.content_type_id === RECORDING_CT_ID)
                .sort((a, b) => new Date(a.added_at).getTime() - new Date(b.added_at).getTime())
            // The mock caller is always author/superuser, so every recording is writable.
            let renamed = 0
            for (const item of items) {
                const rec = item.object_hash ? findRecording(item.object_hash) : undefined
                if (!rec) {
                    continue
                }
                renamed++
                rec.custom_name = `${prefix} ${renamed}`
                item.object_name = resolvedDisplayName(rec)
            }
            return send(res, 200, { renamed, skipped: 0 })
        }
    }

    // DELETE /api/v1/library/collections/{id}/items/{itemId}/
    {
        const m = path.match(/^\/api\/v1\/library\/collections\/(\d+)\/items\/(\d+)\/$/)
        if (m && method === 'DELETE') {
            const collId = Number(m[1])
            const itemId = Number(m[2])
            const idx = _state.collectionItems.findIndex(i => i.id === itemId && i._parent_id === collId)
            if (idx === -1) return notFound(res)
            _state.collectionItems.splice(idx, 1)
            return noContent(res)
        }
    }

    // ── Datasets (mirror of collections) ──────────────────────────────────────

    if (path === '/api/v1/library/datasets/') {
        if (method === 'GET') {
            return send(res, 200, _state.datasets.filter(d => !d.deleted_at))
        }
        if (method === 'POST') {
            const body = await readBody(req)
            const ds: MockGroup = {
                id: _state.seq.ds++,
                name: String(body.name ?? 'Untitled Dataset'),
                description: String(body.description ?? ''),
                parent_id: null,
                author_id: MOCK_USER_ID,
                created_at: new Date().toISOString(),
                modified_at: new Date().toISOString(),
                deleted_at: null,
            }
            _state.datasets.push(ds)
            return send(res, 201, ds)
        }
    }

    // GET|PATCH|DELETE /api/v1/library/datasets/{id}/
    {
        const m = path.match(/^\/api\/v1\/library\/datasets\/(\d+)\/$/)
        if (m) {
            const id = Number(m[1])
            const ds = _state.datasets.find(d => d.id === id && !d.deleted_at)
            if (!ds) return notFound(res)
            if (method === 'GET') return send(res, 200, ds)
            if (method === 'PATCH') {
                const body = await readBody(req)
                if (typeof body.name === 'string') ds.name = body.name
                if (typeof body.description === 'string') ds.description = body.description
                ds.modified_at = new Date().toISOString()
                return send(res, 200, ds)
            }
            if (method === 'DELETE') {
                ds.deleted_at = new Date().toISOString()
                return noContent(res)
            }
        }
    }

    // GET|POST /api/v1/library/datasets/{id}/items/
    {
        const m = path.match(/^\/api\/v1\/library\/datasets\/(\d+)\/items\/$/)
        if (m) {
            const dsId = Number(m[1])
            if (method === 'GET') {
                return send(res, 200, _state.datasetItems.filter(i => i._parent_id === dsId).map(itemOut))
            }
            if (method === 'POST') {
                const body = await readBody(req)
                const ctId = Number(body.content_type_id)
                const objectId = String(body.object_id ?? '').trim()
                if (_state.datasetItems.some(i => i._parent_id === dsId && i.object_id === objectId && i.content_type_id === ctId)) {
                    return conflict(res, 'Item already in dataset.')
                }
                const item: MockItem = {
                    id: _state.seq.item++,
                    _parent_id: dsId,
                    content_type_id: ctId,
                    object_id: objectId,
                    added_at: new Date().toISOString(),
                    ...enrichItem(ctId, objectId),
                }
                _state.datasetItems.push(item)
                return send(res, 201, itemOut(item))
            }
        }
    }

    // DELETE /api/v1/library/datasets/{id}/items/{itemId}/
    {
        const m = path.match(/^\/api\/v1\/library\/datasets\/(\d+)\/items\/(\d+)\/$/)
        if (m && method === 'DELETE') {
            const dsId = Number(m[1])
            const itemId = Number(m[2])
            const idx = _state.datasetItems.findIndex(i => i.id === itemId && i._parent_id === dsId)
            if (idx === -1) return notFound(res)
            _state.datasetItems.splice(idx, 1)
            return noContent(res)
        }
    }

    // GET|POST /api/v1/library/datasets/{id}/access/
    {
        const m = path.match(/^\/api\/v1\/library\/datasets\/(\d+)\/access\/$/)
        if (m) {
            const dsId = Number(m[1])
            if (method === 'GET') {
                return send(res, 200, _state.datasetAccess.filter(a => a._parent_id === dsId).map(accessOut))
            }
            if (method === 'POST') {
                const body = await readBody(req)
                const access: MockAccess = {
                    id: _state.seq.access++,
                    _parent_id: dsId,
                    access_target_id: (body.access_target_id as number | null) ?? null,
                    access_target_group_id: (body.access_target_group_id as number | null) ?? null,
                    public_share_token: (body.public_share_token as string | null) ?? null,
                    can_read: body.can_read !== false,
                    can_write: Boolean(body.can_write),
                    can_share: Boolean(body.can_share),
                    expires_at: (body.expires_at as string | null) ?? null,
                }
                _state.datasetAccess.push(access)
                return send(res, 201, accessOut(access))
            }
        }
    }

    // DELETE /api/v1/library/datasets/{id}/access/{rightId}/
    {
        const m = path.match(/^\/api\/v1\/library\/datasets\/(\d+)\/access\/(\d+)\/$/)
        if (m && method === 'DELETE') {
            const dsId = Number(m[1])
            const rightId = Number(m[2])
            const idx = _state.datasetAccess.findIndex(a => a.id === rightId && a._parent_id === dsId)
            if (idx === -1) return notFound(res)
            _state.datasetAccess.splice(idx, 1)
            return noContent(res)
        }
    }

    return false
}
