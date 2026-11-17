/**
 * Tests for the media type an upload declares from its file name.
 *
 * The server serves a file inline only when its type matches the declared media type, so a wrong declaration does
 * not fail an upload; it leaves a video that downloads instead of playing, or an image the viewer cannot open.
 */
import { describe, expect, it } from 'vitest'
import { mediaTypeForFilename } from './media'

describe('mediaTypeForFilename', () => {
    it.each([
        ['clip.mp4', 'video'],
        ['clip.WEBM', 'video'],
        ['scan.png', 'image'],
        ['scan.JPG', 'image'],
        ['sound.mp3', 'audio'],
        ['sound.wav', 'audio'],
        ['notes.pdf', 'document'],
        ['notes.md', 'document'],
    ])('declares %s as %s', (name, type) => {
        expect(mediaTypeForFilename(name)).toBe(type)
    })

    it('keeps SVG a document, which the server never serves inline', () => {
        expect(mediaTypeForFilename('drawing.svg')).toBe('document')
    })

    it('declares a name without an extension a document', () => {
        expect(mediaTypeForFilename('README')).toBe('document')
    })
})
