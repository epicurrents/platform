/**
 * Client for the annotation API: the code summary of a recording and the registered vocabularies.
 *
 * @package    epicurrents-platform
 */
import { http } from '#lib/http'

/** One code value on a recording, with its vocabulary's name for it when the vocabulary has one. */
export interface CodeCount {
    value: string
    name: string | null
    count: number
}

/**
 * The codes of one standard on a recording. `label` is the registered vocabulary's label, or null for a standard
 * the deployment has not registered. `codes` are ordered by count, then by value.
 */
export interface CodeSummary {
    standard: string
    label: string | null
    count: number
    codes: CodeCount[]
}

/**
 * The distinct codes on a recording's events, interruptions and labels, grouped by standard.
 * @param hash - The recording's hash.
 */
export async function getRecordingCodes(hash: string): Promise<CodeSummary[]> {
    const response = await http.get<CodeSummary[]>(`/annotations/api/v1/recordings/${hash}/codes/`)
    return response.data
}

/** A coding standard the deployment registers. */
export interface Vocabulary {
    standard: string
    label: string
    version: string
    closed: boolean
    confined: boolean
}

/** The coding standards the deployment validates. */
export async function listVocabularies(): Promise<Vocabulary[]> {
    const response = await http.get<Vocabulary[]>('/annotations/api/v1/vocabularies')
    return response.data
}
