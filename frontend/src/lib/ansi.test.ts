/**
 * Terminal escape sequences read into styled runs, as the job page draws update.sh and agent output.
 */

import { describe, expect, it } from 'vitest'

import { ansiSegments } from './ansi'

const ESC = '\u001b'

describe('ansiSegments', () => {
    it('returns plain text as one unstyled run', () => {
        expect(ansiSegments('::step=build\n')).toEqual([{ bold: false, text: '::step=build\n', tone: null }])
    })

    it('reads bold and colour from one sequence and resets on 0', () => {
        expect(ansiSegments(`${ESC}[1;31mERROR:${ESC}[0m The package is refused.`)).toEqual([
            { bold: true, text: 'ERROR:', tone: 'danger' },
            { bold: false, text: ' The package is refused.', tone: null },
        ])
    })

    it('maps the colours the scripts use to semantic tones', () => {
        const segments = ansiSegments(`${ESC}[32m✓${ESC}[0m ${ESC}[34m==>${ESC}[0m ${ESC}[93m!${ESC}[39m`)
        expect(segments.filter(segment => segment.tone).map(segment => [segment.text, segment.tone])).toEqual([
            ['✓', 'success'],
            ['==>', 'brand'],
            ['!', 'warning'],
        ])
    })

    it('draws an unmapped colour in the default tone', () => {
        expect(ansiSegments(`${ESC}[35mmagenta`)).toEqual([{ bold: false, text: 'magenta', tone: null }])
    })

    it('drops control sequences that carry no style, OSC titles included', () => {
        const text = `${ESC}[2K${ESC}[1Aline${ESC}]0;title${ESC}\\ ${ESC}]8;;https://example.org\u0007link`
        expect(ansiSegments(text)).toEqual([{ bold: false, text: 'line link', tone: null }])
    })

    it('treats an empty SGR as a reset and merges runs that share a style', () => {
        expect(ansiSegments(`${ESC}[1mbold${ESC}[mplain${ESC}[22m more`)).toEqual([
            { bold: true, text: 'bold', tone: null },
            { bold: false, text: 'plain more', tone: null },
        ])
    })

    it('keeps text that only looks like an escape', () => {
        expect(ansiSegments('[1;34m without the escape byte')).toEqual([
            { bold: false, text: '[1;34m without the escape byte', tone: null },
        ])
    })
})
