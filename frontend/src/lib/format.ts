/**
 * Formatting for the recording quantities the listings, the browser and the details drawer show.
 *
 * @package    epicurrents-platform
 */

/**
 * A duration in the two largest units that apply, e.g. "42 s", "3 m 20 s", "1 h 5 m".
 * @param secs - Duration in seconds.
 */
export function formatDuration (secs: number): string {
    if (secs < 60) {
        return `${secs.toFixed(0)} s`
    }
    if (secs < 3600) {
        return `${Math.floor(secs / 60)} m ${Math.floor(secs % 60)} s`
    }
    return `${Math.floor(secs / 3600)} h ${Math.floor((secs % 3600) / 60)} m`
}

/**
 * A file size in kilobytes below a megabyte and in megabytes above.
 * @param bytes - Size in bytes.
 */
export function formatSize (bytes: number): string {
    if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(0)} KB`
    }
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/**
 * A sampling rate in hertz, or the range of rates when the channels differ, e.g. "256 Hz", "200–512 Hz".
 * @param max - The highest rate among the signal channels, or null when unknown.
 * @param min - The lowest rate, or null when unknown.
 */
export function formatSamplingRate (max: number | null, min: number | null): string {
    if (max === null) {
        return ''
    }
    const hz = (rate: number) => Number(rate.toFixed(2)).toString()
    if (min === null || min === max) {
        return `${hz(max)} Hz`
    }
    return `${hz(min)}–${hz(max)} Hz`
}

/**
 * Month and year, e.g. "Nov 2026". For a date the server serves at month precision, such as a release month, which
 * it sends as midnight UTC on the first: read in UTC, so a reader west of Greenwich is not shown the month before.
 * @param iso - ISO 8601 timestamp from the API.
 */
export function formatMonth (iso: string): string {
    const parsed = new Date(iso)
    if (Number.isNaN(parsed.getTime())) {
        return iso
    }
    return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', timeZone: 'UTC' })
}

/**
 * A subject value as the browser shows it: a choice as it is, a number to two decimals at most, e.g. "1.49".
 * @param value - The member's value for a field, or undefined when it has none.
 */
export function formatSubjectValue (value: string | number | undefined): string {
    if (value === undefined) {
        return ''
    }
    return typeof value === 'number' ? Number(value.toFixed(2)).toString() : value
}
