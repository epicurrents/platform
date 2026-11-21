/**
 * The study link the dicom plugin hands to OHIF: it must name a mode route and the dicomjson data source, or OHIF
 * opens its worklist instead of the study.
 */

import { describe, expect, it } from 'vitest'
import { dicomApi } from './api'

describe('ohifViewerUrl', () => {
    it('opens the longitudinal mode on the dicomjson source with the study JSON as url', () => {
        const url = new URL(dicomApi.ohifViewerUrl('abc123'), window.location.origin)
        expect(url.pathname).toBe('/plugin/dicom/ohif/viewer/dicomjson')
        expect(url.searchParams.get('url')).toBe(
            `${window.location.origin}/plugin/dicom/api/v1/dicom/studies/abc123/ohif-json/`
        )
    })
})
