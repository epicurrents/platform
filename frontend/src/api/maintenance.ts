/**
 * Client for the maintenance API at `/api/v1/maintenance/`.
 *
 * Reads require staff and writes require superuser, and the whole surface
 * answers 404 while the deployment has `REMOTE_MAINTENANCE_ENABLED` off — the
 * tab hides itself on that answer rather than showing an error. A request
 * names a registered operation and its arguments, never a command; what runs
 * is decided by the server's registry.
 *
 * @package    epicurrents-platform
 */

import { http } from '#lib/http'

/** The host agent as its heartbeat describes it; `installed` false when no heartbeat exists. */
export interface AgentStatus {
    installed: boolean
    enabled: boolean | null
    version: string | null
    runtime: string | null
    last_run: string | null
    stale: boolean | null
}

/** The maintenance flag, when the platform is locked. */
export interface MaintenanceLockInfo {
    phase: 'updating' | 'verifying' | 'rolling_back' | string
    since: string | null
    expected_until: string | null
    message: string
    job_id: string | null
}

/** How the caller confirms a sensitive request, or why they cannot. */
export interface StepUpInfo {
    method: 'password' | 'password+totp' | 'totp' | null
    available: boolean
    reason: string | null
}

export interface MaintenanceStatus {
    remote_maintenance_enabled: boolean
    remote_update_enabled: boolean
    installed_version: string
    server_now: string
    spool_writable: boolean
    release_key_present: boolean
    agent: AgentStatus
    lock: MaintenanceLockInfo | null
    in_flight_job: string | null
    step_up: StepUpInfo
}

/** One property of an operation's argument schema, as the server publishes it. */
export interface ArgSchemaProperty {
    type?: string
    anyOf?: { type: string }[]
    default?: unknown
    description?: string
    minimum?: number
    maximum?: number
    pattern?: string
    title?: string
}

/** The JSON schema of an operation's arguments; only the parts the form reads. */
export interface ArgSchema {
    properties?: Record<string, ArgSchemaProperty>
    required?: string[]
}

export interface MaintenanceOperation {
    key: string
    executor: 'celery' | 'host'
    label: string
    description: string
    requires_step_up: boolean
    /** False for a host-tier operation while `REMOTE_UPDATE_ENABLED` is off. */
    available: boolean
    args_schema: ArgSchema
}

export type JobState =
    | 'requested'
    | 'accepted'
    | 'running'
    | 'awaiting_verification'
    | 'succeeded'
    | 'failed'
    | 'cancelled'
    | 'rolling_back'
    | 'rolled_back'
    | 'rollback_failed'

export interface MaintenanceJob {
    job_id: string
    operation: string
    executor: 'celery' | 'host'
    state: JobState
    reason: string
    step: string
    in_flight: boolean
    requested_by: string | null
    package_sha256: string | null
    args: Record<string, unknown>
    created_at: string
    started_at: string | null
    finished_at: string | null
    verify_deadline: string | null
    verify_requested_at: string | null
    rollback_requested_at: string | null
    installed_version_before: string
    target_version: string
    running_version: string
    snapshot: string
    post_snapshot: string
    /** Present for superusers only. */
    output: string | null
}

export interface JobLog {
    job_id: string
    log: string
    truncated: boolean
    bytes: number
}

/** Credentials for a step-up confirmation; which ones are needed is what `StepUpInfo.method` says. */
export interface StepUpCredentials {
    password?: string
    totp_code?: string
}

export interface JobRequest extends StepUpCredentials {
    operation: string
    args: Record<string, unknown>
}

const BASE = '/api/v1/maintenance'

export async function fetchMaintenanceStatus(): Promise<MaintenanceStatus> {
    const response = await http.get<MaintenanceStatus>(`${BASE}/status`)
    return response.data
}

export async function listOperations(): Promise<MaintenanceOperation[]> {
    const response = await http.get<MaintenanceOperation[]>(`${BASE}/operations`)
    return response.data
}

export async function listJobs(): Promise<MaintenanceJob[]> {
    const response = await http.get<MaintenanceJob[]>(`${BASE}/jobs`)
    return response.data
}

export async function fetchJob(jobId: string): Promise<MaintenanceJob> {
    const response = await http.get<MaintenanceJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}`)
    return response.data
}

export async function fetchJobLog(jobId: string): Promise<JobLog> {
    const response = await http.get<JobLog>(`${BASE}/jobs/${encodeURIComponent(jobId)}/log`)
    return response.data
}

/** Request an operation. Resolves to the job the server accepted (202). */
export async function createJob(request: JobRequest): Promise<MaintenanceJob> {
    const response = await http.post<MaintenanceJob>(`${BASE}/jobs`, request)
    return response.data
}

export async function cancelJob(jobId: string): Promise<MaintenanceJob> {
    const response = await http.post<MaintenanceJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/cancel`, {})
    return response.data
}

/** Confirm an update. The server asks for the password only, never the second factor. */
export async function verifyJob(jobId: string, credentials: StepUpCredentials): Promise<MaintenanceJob> {
    const response = await http.post<MaintenanceJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/verify`, credentials)
    return response.data
}

/** Ask for a rollback. Full step-up: password plus the second factor when enrolled. */
export async function rollbackJob(jobId: string, credentials: StepUpCredentials): Promise<MaintenanceJob> {
    const response = await http.post<MaintenanceJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/rollback`, credentials)
    return response.data
}

/**
 * Whether a failed request is the maintenance API answering 404 because the
 * feature is off, as opposed to a job that does not exist. The status and
 * operations routes have nothing to be missing, so a 404 from them is the gate.
 */
export function isFeatureDisabled(error: unknown): boolean {
    const status = (error as { response?: { status?: number } })?.response?.status
    return status === 404
}
