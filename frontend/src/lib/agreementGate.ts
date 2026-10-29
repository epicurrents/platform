/**
 * Notice the API refusing an account that has not accepted the user agreement.
 *
 * The route guard sends such an account to the agreement page before any view loads, but a version published while
 * the account is signed in is first noticed by a request. The gate middleware answers it with
 * `403 {"code": "agreement_required"}`; the HTTP wrapper reports that here, and the app shell, which registers the
 * handler, takes the account to the agreement page. Kept below the stores so the wrapper imports nothing above it.
 *
 * @package    epicurrents-platform
 */

/** The `code` the gate middleware answers with. */
export const AGREEMENT_REQUIRED = 'agreement_required'

let handler: (() => void) | null = null

/**
 * Whether a response body is the gate's refusal.
 *
 * @param data - the body of a 403 response, of whatever shape it has.
 */
export function isAgreementRefusal (data: unknown): boolean {
    return (data as { code?: unknown } | null)?.code === AGREEMENT_REQUIRED
}

/** Report a refusal to the registered handler, if there is one. */
export function reportAgreementRefusal () {
    handler?.()
}

/**
 * Set what happens when the API refuses for want of an accepted agreement; one handler at a time.
 *
 * @param next - the handler, or null to remove it.
 */
export function setAgreementRefusalHandler (next: (() => void) | null) {
    handler = next
}
