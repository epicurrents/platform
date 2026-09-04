import { http } from '#lib/http'

export type DeploymentMode = 'development' | 'production' | 'unset'

export interface HealthInfo {
    status: string
    mode: DeploymentMode
    debug: boolean
}

/**
 * Read the backend's deployment posture from `/api/v1/health`.
 *
 * The `mode` field drives the App.vue dev-mode banner; the `debug` flag
 * is exposed for completeness (e.g. surfacing it in operator views).
 */
export async function fetchHealth(): Promise<HealthInfo> {
    const response = await http.get<HealthInfo>('/api/v1/health')
    return response.data
}
