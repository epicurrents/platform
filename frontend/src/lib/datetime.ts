/**
 * Locale-aware formatting for the ISO 8601 timestamps the API returns.
 *
 * Both helpers hand the raw string back when it will not parse, rather than
 * rendering the browser's "Invalid Date". A timestamp the platform cannot read
 * is worth showing verbatim: it is the only clue to what the server actually
 * sent, and an operator can act on it where a fixed error string tells them
 * nothing.
 *
 * The locale is deliberately left to the browser (`undefined`), so dates read
 * the way the reader expects rather than the way the deployment was configured.
 *
 * @package    epicurrents-platform
 */

/** Parse an ISO timestamp, or `null` when it is not a usable date. */
function parseIso (iso: string): Date | null {
    const parsed = new Date(iso)
    return Number.isNaN(parsed.getTime()) ? null : parsed
}

/**
 * Date alone, e.g. "9 Sep 2026". For timestamps where the time of day carries
 * nothing the reader needs — a creation date, an account's join date.
 *
 * @param iso - ISO 8601 timestamp from the API.
 */
export function formatDate (iso: string): string {
    const parsed = parseIso(iso)
    if (parsed === null) {
        return iso
    }
    return parsed.toLocaleDateString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
    })
}

/**
 * Date and time, e.g. "9 Sep 2026, 07:45". For timestamps an operator reads to
 * place an event within a day, such as a last sign-in.
 *
 * @param iso - ISO 8601 timestamp from the API.
 */
export function formatDateTime (iso: string): string {
    const parsed = parseIso(iso)
    if (parsed === null) {
        return iso
    }
    return parsed.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })
}
