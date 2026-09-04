import { http } from '#lib/http'

export interface AuthUser {
    id: number
    username: string
    email: string
    first_name: string
    last_name: string
    is_staff: boolean
    is_superuser: boolean
    /** Whether the account has a confirmed second factor on its password login. */
    is_2fa_enabled: boolean
    /** Project-supplied roles inherited through group membership, keyed by the role key the active project registered. Empty when the deployment defines no roles. */
    roles?: Record<string, string[]>
}

export interface ProfileUpdate {
    email?: string
    first_name?: string
    last_name?: string
}

/** Auth-state probe result: `/api/v1/user/me` answers with HTTP 200 either way. */
interface AuthState {
    authenticated: boolean
    user: AuthUser | null
}

/**
 * Probe the current auth state. Resolves to the user when signed in and `null`
 * when logged out — both are HTTP 200, so a logged-out probe is a normal answer
 * rather than a console error. Only network / 5xx failures reject.
 */
export async function fetchMe(): Promise<AuthUser | null> {
    const response = await http.get<AuthState>('/api/v1/user/me')
    return response.data.user
}

/**
 * Outcome of a login attempt. A correct password is not the same as a session:
 * an account with a second factor comes back with `two_factor_required` and no
 * user, and only `submitTwoFactorCode` finishes it.
 */
export interface LoginResult {
    authenticated: boolean
    two_factor_required: boolean
    /**
     * The password was right, the deployment requires a second factor and this
     * account has none. Distinct from `two_factor_required` because it leads to
     * a different screen: there is no code to ask for yet, so the caller must
     * call `startLoginEnrolment` first and scan the result.
     */
    two_factor_enrolment_required: boolean
    /** Returned once, when a login completes a first-time enrolment. */
    backup_codes: string[] | null
    user: AuthUser | null
}

/** Secret for an account that must enrol before it may sign in. */
export interface LoginEnrolment {
    secret: string
    provisioning_uri: string
}

export async function login(username: string, password: string): Promise<LoginResult> {
    const response = await http.post<LoginResult>('/api/v1/user/login', { username, password })
    return response.data
}

/**
 * Mint a secret for a login that cannot proceed until the account enrols.
 *
 * Reachable only between a correct password and a session, on the strength of
 * the pending-login marker the server holds. Every other enrolment endpoint
 * needs a session, which is exactly what this login is withholding.
 */
export async function startLoginEnrolment(): Promise<LoginEnrolment> {
    const response = await http.post<LoginEnrolment>('/api/v1/user/login/2fa/setup')
    return response.data
}

/** Submit a TOTP or backup code against the login this session already started. */
export async function submitTwoFactorCode(code: string): Promise<LoginResult> {
    const response = await http.post<LoginResult>('/api/v1/user/login/2fa', { code })
    return response.data
}

export async function logout(): Promise<void> {
    await http.post('/api/v1/user/logout')
}

export async function updateProfile(payload: ProfileUpdate): Promise<AuthUser> {
    const response = await http.patch<AuthUser>('/api/v1/user/me', payload)
    return response.data
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await http.post('/api/v1/user/me/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
    })
}

export async function requestPasswordReset(email: string): Promise<void> {
    await http.post('/api/v1/user/reset-password', { email })
}

export async function confirmPasswordReset(uid: string, token: string, newPassword: string): Promise<void> {
    await http.post('/api/v1/user/reset-password/confirm', { uid, token, new_password: newPassword })
}

/** The caller's second-factor state, as `GET /me/2fa` reports it. */
export interface TwoFactorStatus {
    enabled: boolean
    confirmed_at: string | null
    backup_codes_remaining: number
}

/**
 * A pending enrolment. `provisioning_uri` goes into the QR code; `secret` is the
 * same value in the form an authenticator that cannot scan will accept typed.
 */
export interface TwoFactorEnrolment {
    secret: string
    provisioning_uri: string
}

export async function fetchTwoFactorStatus(): Promise<TwoFactorStatus> {
    const response = await http.get<TwoFactorStatus>('/api/v1/user/me/2fa')
    return response.data
}

export async function startTwoFactorEnrolment(password: string): Promise<TwoFactorEnrolment> {
    const response = await http.post<TwoFactorEnrolment>('/api/v1/user/me/2fa', { password })
    return response.data
}

/** Activate a pending enrolment. Resolves to the recovery codes, shown once. */
export async function confirmTwoFactorEnrolment(code: string): Promise<string[]> {
    const response = await http.post<{ backup_codes: string[] }>('/api/v1/user/me/2fa/confirm', { code })
    return response.data.backup_codes
}

export async function regenerateBackupCodes(password: string): Promise<string[]> {
    const response = await http.post<{ backup_codes: string[] }>('/api/v1/user/me/2fa/backup-codes', { password })
    return response.data.backup_codes
}

export async function disableTwoFactor(password: string): Promise<void> {
    await http.post('/api/v1/user/me/2fa/disable', { password })
}

export interface UserSearchResult {
    id: number
    username: string
    first_name: string
    last_name: string
}

export async function searchUsers(q: string): Promise<UserSearchResult[]> {
    const response = await http.get<UserSearchResult[]>('/api/v1/user/search', { params: { q } })
    return response.data
}

export interface Group {
    id: number
    name: string
}

export async function listGroups(): Promise<Group[]> {
    const response = await http.get<Group[]>('/api/v1/user/groups')
    return response.data
}

/** A configured external-login provider offered on the sign-in screen. */
export interface OIDCProvider {
    name: string
    label: string
    /** Backend path that starts the redirect flow; navigate to it (not XHR). */
    login_url: string
}

export interface AuthConfig {
    oidc_providers: OIDCProvider[]
}

export async function fetchAuthConfig(): Promise<AuthConfig> {
    const response = await http.get<AuthConfig>('/api/v1/user/auth-config')
    return response.data
}
