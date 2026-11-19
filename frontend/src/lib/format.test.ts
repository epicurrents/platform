/**
 * Tests for the recording quantity formatters.
 *
 * @package    epicurrents-platform
 */
import { describe, expect, it } from 'vitest'
import { formatDuration, formatMonth, formatSamplingRate, formatSize, formatSubjectValue } from './format'

describe('formatDuration', () => {
    it('uses the two largest units that apply', () => {
        expect(formatDuration(42)).toBe('42 s')
        expect(formatDuration(200)).toBe('3 m 20 s')
        expect(formatDuration(3900)).toBe('1 h 5 m')
    })
})

describe('formatSize', () => {
    it('switches to megabytes at a megabyte', () => {
        expect(formatSize(2048)).toBe('2 KB')
        expect(formatSize(3 * 1024 * 1024)).toBe('3.0 MB')
    })
})

describe('formatSamplingRate', () => {
    it('shows one rate when the channels agree and a range when they differ', () => {
        expect(formatSamplingRate(256, 256)).toBe('256 Hz')
        expect(formatSamplingRate(512, null)).toBe('512 Hz')
        expect(formatSamplingRate(512, 200)).toBe('200–512 Hz')
        expect(formatSamplingRate(0.5, 0.25)).toBe('0.25–0.5 Hz')
    })

    it('is empty while the rate is unknown', () => {
        expect(formatSamplingRate(null, null)).toBe('')
    })
})

describe('formatMonth', () => {
    it('hands back a timestamp it cannot parse', () => {
        expect(formatMonth('not a date')).toBe('not a date')
    })

    it('reads the first of a month in UTC', () => {
        const november = new Date(Date.UTC(2026, 10, 15)).toLocaleDateString(undefined, {
            month: 'short', timeZone: 'UTC', year: 'numeric',
        })
        expect(formatMonth('2026-11-01T00:00:00Z')).toBe(november)
    })
})

describe('formatSubjectValue', () => {
    it('shows a choice as it is and a number to two decimals at most', () => {
        expect(formatSubjectValue('65+')).toBe('65+')
        expect(formatSubjectValue(1.487123288)).toBe('1.49')
        expect(formatSubjectValue(12)).toBe('12')
        expect(formatSubjectValue(undefined)).toBe('')
    })
})
