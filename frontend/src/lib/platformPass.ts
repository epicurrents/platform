/**
 * What the platform did to an attachment's or a registered dataset member's stored content, as the listings record it.
 *
 * A row's `platform_pass` names the pass applied, or `none` for content stored as uploaded. It is a record of what
 * was done and never an assurance about the content, so the one thing a client reads out of it is the absence of a
 * pass: a member stored as uploaded is marked wherever it can be opened or shared onward.
 */

/** The `platform_pass` of content the platform stored as uploaded. */
export const PASS_NONE = 'none'

/**
 * Whether a listed row is content the platform stored as uploaded. A row that names no pass at all (a recording, a
 * media file, a type declaring none, a server that predates the field) is not marked: nothing is known about it here.
 * @param row - A dataset item or an attachment as its listing gives it.
 */
export function storedAsUploaded(row: { platform_pass?: string | null }) {
    return row.platform_pass === PASS_NONE
}
