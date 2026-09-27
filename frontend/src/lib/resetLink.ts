/**
 * The credential a password-reset or invitation link carries, read out of the URL fragment and then removed from it.
 *
 * The link is `/reset-password#uid=<b64>&token=<token>` (with `&welcome=1` for
 * an invitation). A fragment is never sent to the server, so the token stays
 * out of access logs and of the `Referer` of anything the page loads; reading
 * it once and replacing the history entry keeps it out of the address bar,
 * the history and a bookmark as well. The values live in memory only.
 *
 * @package    epicurrents-platform
 */

export interface ResetLink {
    uid: string
    token: string
    /** Set by the invitation mail: the holder has never signed in, which changes what a dead link says. */
    welcome: boolean
}

/**
 * Read the reset credential from the fragment and strip the fragment from the current history entry.
 *
 * Returns `null` when the fragment does not carry both `uid` and `token`; the
 * fragment is stripped either way. The history entry's state is kept, since
 * the router stores its own bookkeeping there.
 *
 * @param loc - the location to read; the window's by default.
 * @param hist - the history whose entry is replaced; the window's by default.
 */
export function consumeResetFragment(
    loc: Location = window.location,
    hist: History = window.history,
): ResetLink | null {
    const fragment = loc.hash.startsWith('#') ? loc.hash.slice(1) : loc.hash
    if (!fragment) {
        return null
    }
    const params = new URLSearchParams(fragment)
    hist.replaceState(hist.state, '', loc.pathname + loc.search)
    const uid = params.get('uid') ?? ''
    const token = params.get('token') ?? ''
    if (!uid || !token) {
        return null
    }
    return { uid, token, welcome: params.get('welcome') === '1' }
}
