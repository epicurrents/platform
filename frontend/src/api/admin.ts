/**
 * Client for the account and group administration API at `/api/v1/user/admin/`.
 *
 * Reads require staff and writes require superuser, and the server decides
 * both — this module never pre-empts a refusal, it reports the one that comes
 * back. There is deliberately no delete-account call: `erase_user` on the host
 * is the sanctioned path, because it also unlinks owned recording and media
 * files that FK cascade never touches.
 *
 * @package    epicurrents-platform
 */

import { http } from '#lib/http'

/** A group as it appears inside an account payload. */
export interface GroupRef {
    id: number
    name: string
}

/**
 * One account as the administration surface sees it.
 *
 * Carries `email`, which the ordinary `UserSearchResult` withholds. That makes
 * this a staff-gated payload: do not render it from a component that is
 * reachable without the staff gate.
 */
export interface Account {
    id: number
    username: string
    email: string
    first_name: string
    last_name: string
    is_active: boolean
    is_staff: boolean
    is_superuser: boolean
    is_2fa_enabled: boolean
    date_joined: string
    last_login: string | null
    groups: GroupRef[]
    /** Roles inherited through group membership, keyed by role key. Read-only here — roles belong to groups. */
    roles: Record<string, string[]>
}

/** New-account payload. Only `username` and `password` are required. */
export interface AccountCreate {
    username: string
    password: string
    email?: string
    first_name?: string
    last_name?: string
    is_active?: boolean
    is_staff?: boolean
    is_superuser?: boolean
}

/** Partial account edit — omitted fields are left alone. Username is not editable. */
export interface AccountUpdate {
    email?: string
    first_name?: string
    last_name?: string
    is_active?: boolean
    is_staff?: boolean
    is_superuser?: boolean
}

/** A group with its roles and the two counts that decide whether it can be deleted. */
export interface GroupDetail {
    id: number
    name: string
    member_count: number
    grant_count: number
    roles: Record<string, string | null>
}

/** A project-supplied role and the values it accepts, as `[value, label]` pairs. */
export interface RoleProvider {
    key: string
    label: string
    choices: string[][]
}

/** Paging window for the account roster. The endpoint returns a bare list, so there is no total to read. */
export interface AccountQuery {
    q?: string
    limit?: number
    offset?: number
}

export async function listRoleProviders(): Promise<RoleProvider[]> {
    const response = await http.get<RoleProvider[]>('/api/v1/user/admin/roles')
    return response.data
}

export async function listAccounts(query: AccountQuery = {}): Promise<Account[]> {
    const response = await http.get<Account[]>('/api/v1/user/admin/accounts', { params: query })
    return response.data
}

export async function fetchAccount(accountId: number): Promise<Account> {
    const response = await http.get<Account>(`/api/v1/user/admin/accounts/${accountId}`)
    return response.data
}

export async function createAccount(payload: AccountCreate): Promise<Account> {
    const response = await http.post<Account>('/api/v1/user/admin/accounts', payload)
    return response.data
}

export async function updateAccount(accountId: number, payload: AccountUpdate): Promise<Account> {
    const response = await http.patch<Account>(`/api/v1/user/admin/accounts/${accountId}`, payload)
    return response.data
}

/**
 * Set another account's password.
 *
 * Deliberately does not end that account's open sessions — deactivation is the
 * control for a suspected compromise, and that does flush them. Copy around
 * this call must not imply otherwise.
 */
export async function setAccountPassword(accountId: number, newPassword: string): Promise<void> {
    await http.post(`/api/v1/user/admin/accounts/${accountId}/password`, { new_password: newPassword })
}

/** Clear an account's second factor, so the holder can enrol again at next sign-in. */
export async function resetAccountTwoFactor(accountId: number): Promise<void> {
    await http.delete(`/api/v1/user/admin/accounts/${accountId}/2fa`)
}

/**
 * Replace an account's group membership with exactly `groupIds`.
 *
 * The only membership write the client makes. `PUT /groups/{id}/members` does
 * the same thing from the group side and is deliberately not wrapped: it takes
 * a whole-membership replacement, which a UI can only build by listing every
 * user, and the account roster it would have to list is capped — so members
 * past the cap would be dropped by an operator who never saw them. Assigning
 * groups to a user is also the smaller list of the two in any real deployment.
 */
export async function setAccountGroups(accountId: number, groupIds: number[]): Promise<Account> {
    const response = await http.put<Account>(`/api/v1/user/admin/accounts/${accountId}/groups`, {
        group_ids: groupIds,
    })
    return response.data
}

export async function listGroups(): Promise<GroupDetail[]> {
    const response = await http.get<GroupDetail[]>('/api/v1/user/admin/groups')
    return response.data
}

export async function createGroup(name: string): Promise<GroupDetail> {
    const response = await http.post<GroupDetail>('/api/v1/user/admin/groups', { name })
    return response.data
}

/**
 * Rename a group and/or write its roles.
 *
 * `roles` is a partial map and the danger is padding it, not omitting from it:
 * the server leaves an absent key untouched and reads an explicit `null` as
 * "clear this role". Build the map with `rolesPayload` rather than by hand.
 */
export async function updateGroup(
    groupId: number,
    payload: { name?: string, roles?: Record<string, string | null> },
): Promise<GroupDetail> {
    const response = await http.patch<GroupDetail>(`/api/v1/user/admin/groups/${groupId}`, payload)
    return response.data
}

/** Delete a group. Refused with 409 while access grants still target it. */
export async function deleteGroup(groupId: number): Promise<void> {
    await http.delete(`/api/v1/user/admin/groups/${groupId}`)
}

/**
 * Build the `roles` map for `updateGroup` from the selectors a form actually rendered.
 *
 * The partial-map trap lives here. A form that submits every key it knows about,
 * padded with `null` for the ones it did not render, clears exactly those roles
 * on the server — so the payload must carry the rendered keys and nothing else.
 * An empty string from an unset selector is the deliberate "clear this role"
 * value and is sent as `null`; a key the deployment does not define is dropped.
 *
 * @param rendered - the role keys whose selectors this form put on screen, from `listRoleProviders`.
 * @param values - current selector values, keyed by role key; `''` means the operator chose no role.
 */
export function rolesPayload(rendered: string[], values: Record<string, string>): Record<string, string | null> {
    const payload: Record<string, string | null> = {}
    for (const key of rendered) {
        const value = values[key] ?? ''
        payload[key] = value === '' ? null : value
    }
    return payload
}
