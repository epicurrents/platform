/**
 * Icon registrations for the *dicom* plugin.
 *
 * Icons are merged with the base icon registry at app startup.  If a name
 * already exists in base icons, the base icon is kept and the plugin entry
 * is ignored.
 *
 * Each value is a complete `<svg>` element as a raw string — the resolver in
 * `main.ts` serves it as a data URI, so the outer element is required (the
 * same convention as the base icons in `src/icons.ts`).
 */

const icons: Record<string, string> = {
    // Radiology / DICOM viewer icon — a stylised CT cross-section.
    dicom: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
        <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 1a6 6 0 1 1 0 12A6 6 0 0 1 8 2z"/>
        <path d="M5.5 5.5h5v5h-5z" opacity=".4"/>
        <path d="M7.5 4v8M4 7.5h8" stroke="currentColor" stroke-width="1" fill="none"/>
    </svg>`,
}

export default icons
