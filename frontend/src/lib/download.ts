/**
 * Save an API response to a file on the user's machine.
 *
 * Navigating to a download endpoint directly would be simpler, but an error response then arrives
 * as a saved file rather than a message: the browser writes the 403 body to disk and the user is
 * left holding a JSON fragment. Fetching the blob through the shared axios instance keeps the
 * failure path in JavaScript, where a toast can be raised instead.
 *
 * @package    epicurrents-platform
 */

import { http } from '#lib/http'

/**
 * Filename fallback used when the response carries no usable `Content-Disposition`. Extensionless
 * on purpose — guessing the wrong extension is worse than letting the OS ask.
 */
const FALLBACK_FILENAME = 'download'

/**
 * GET *url* and save the response as a file.
 *
 * The server-supplied `Content-Disposition` filename wins; `fallbackName` covers the case where
 * the header is absent or unparseable.
 *
 * @param url - API path to fetch, relative to the http instance base URL.
 * @param params - Query parameters. Array values repeat the key, matching how Django reads them.
 * @param fallbackName - Filename to use when the response does not name one.
 */
export async function downloadFile(
    url: string,
    params: Record<string, unknown> = {},
    fallbackName: string = FALLBACK_FILENAME,
): Promise<void> {
    const response = await http.get<Blob>(url, {
        params,
        responseType: 'blob',
        // Repeat the key for array values (?recording=a&recording=b) rather than axios's default
        // bracketed form, which Django's QueryDict does not unpack into a list.
        paramsSerializer: { indexes: null },
    })
    const filename = filenameFromDisposition(response.headers?.['content-disposition']) || fallbackName
    saveBlob(response.data, filename)
}

/**
 * Extract the filename from a `Content-Disposition` header value.
 *
 * Prefers the RFC 5987 `filename*=UTF-8''…` form Django emits for non-ASCII names, falling back to
 * the plain quoted `filename=`. Returns an empty string when neither is present.
 *
 * @param disposition - Raw header value, or undefined when the response carried none.
 */
export function filenameFromDisposition(disposition: string | undefined): string {
    if (!disposition) {
        return ''
    }
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
    if (encoded) {
        try {
            return decodeURIComponent(encoded[1])
        } catch {
            // A malformed percent-escape is not worth failing the download over; fall through to
            // the plain form below.
        }
    }
    const plain = /filename="?([^";]+)"?/i.exec(disposition)
    return plain ? plain[1] : ''
}

/** Trigger a save dialog for *blob* under *filename*. */
function saveBlob(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    document.body.removeChild(anchor)
    // Revoking synchronously can cancel the download in some browsers; defer past the click.
    setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * Read an error body that arrived as a Blob.
 *
 * With `responseType: 'blob'` axios hands back the error payload as a Blob too, so the usual
 * `err.response.data.detail` read yields undefined. Views call this to recover the message.
 *
 * @param data - The `response.data` of a failed blob request.
 */
export async function readBlobError(data: unknown): Promise<string> {
    if (!(data instanceof Blob)) {
        return ''
    }
    try {
        const parsed = JSON.parse(await data.text())
        return typeof parsed?.detail === 'string' ? parsed.detail : ''
    } catch {
        return ''
    }
}
