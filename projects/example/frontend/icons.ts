/**
 * Icon registrations for the example project.
 *
 * Icons are raw SVG strings merged into the base registry at app startup; if a name already
 * exists in the base icons, the base icon wins and the project entry is ignored, so pick names
 * that do not collide unless shadow-tolerance is the intent.
 *
 * The template inlines its one icon so the scaffold needs no icon dependency of its own. A
 * project with more than a couple of icons should instead declare an icon package in its own
 * `frontend/package.json` and import the SVGs with `?raw`, the way the real projects do:
 *
 * ```ts
 * import noteAlt from '@material-symbols/svg-400/outlined/note_alt.svg?raw'
 * ```
 */

const noteSvg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor">' +
    '<path d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h560q33 0 56.5 23.5' +
    'T840-760v560q0 33-23.5 56.5T760-120H200Zm0-80h560v-560H200v560Zm80-80h240v-80H280v80Zm0' +
    '-160h400v-80H280v80Zm0-160h400v-80H280v80Z"/></svg>'

const icons: Record<string, string> = {
    'example-note': noteSvg,
}

export default icons
