/**
 * Terminal output as styled text: the SGR escape sequences update.sh and the host agent print, read into runs a
 * template renders as spans.
 *
 * Only bold and the four colours those scripts use are kept, each mapped to a semantic tone so it follows the colour
 * mode; every other control sequence is dropped. The result is text and class names, never markup, so a log cannot
 * inject anything into the page.
 *
 * @package    epicurrents-platform
 */

/** A run of text and the style it is drawn in. */
export interface AnsiSegment {
    bold: boolean
    text: string
    /** The semantic tone of a coloured run; `null` for the default colour. */
    tone: AnsiTone | null
}

export type AnsiTone = 'brand' | 'danger' | 'success' | 'warning'

/** Standard and bright foreground codes, by the tone each is drawn in. Codes not listed draw in the default colour. */
const TONES: Record<number, AnsiTone> = {
    31: 'danger',
    32: 'success',
    33: 'warning',
    34: 'brand',
    91: 'danger',
    92: 'success',
    93: 'warning',
    94: 'brand',
}

/** A CSI sequence (SGR or any other), an OSC sequence ended by BEL or ST, or a lone two-character escape. */
// eslint-disable-next-line no-control-regex
const ESCAPE = /\u001b\[([0-9;?]*)([@-~])|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-_]/g

/**
 * Split `text` into styled runs. Adjacent runs never share a style, and runs of empty text are not returned.
 *
 * @param text - terminal output, possibly holding escape sequences.
 */
export function ansiSegments (text: string): AnsiSegment[] {
    const segments: AnsiSegment[] = []
    let bold = false
    let tone: AnsiTone | null = null
    let last = 0
    const push = (run: string) => {
        if (!run) {
            return
        }
        const previous = segments[segments.length - 1]
        if (previous && previous.bold === bold && previous.tone === tone) {
            previous.text += run
        } else {
            segments.push({ bold, text: run, tone })
        }
    }
    for (const match of text.matchAll(ESCAPE)) {
        push(text.slice(last, match.index))
        last = match.index + match[0].length
        // Only SGR (`m`) carries style; any other sequence is dropped with nothing to apply.
        if (match[2] !== 'm') {
            continue
        }
        const codes = (match[1] ?? '').split(';').map(code => (code === '' ? 0 : Number(code)))
        for (const code of codes) {
            if (code === 0) {
                bold = false
                tone = null
            } else if (code === 1) {
                bold = true
            } else if (code === 22) {
                bold = false
            } else if (code === 39) {
                tone = null
            } else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
                tone = TONES[code] ?? null
            }
        }
    }
    push(text.slice(last))
    return segments
}
