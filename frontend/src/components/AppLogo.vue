<script setup lang="ts">
// ---------------------------------------------------------------------------
// The Epicurrents mark, drawn inline so it can follow the active colour mode.
// The geometry is a verbatim port of src/assets/logo.vue in the
// epicurrents.github.io repository, so the two stay re-syncable by copying the
// paths across. Two things differ: the mark's fixed blues (#0057af arcs,
// #007fff nodes) become WA brand tokens in the style block below, and the
// Inkscape metadata and layer title are dropped — an SVG <title> would become
// this element's accessible name, which the brand link's own text already is.
// ---------------------------------------------------------------------------

withDefaults(defineProps<{
    /** Outline stroke width in viewBox units; the viewBox is 512 square, so small values vanish at icon sizes. */
    strokeWidth?: number
}>(), {
    strokeWidth: 7.5,
})
</script>

<template>
    <svg aria-hidden="true" class="logo" version="1.1" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
        <g fill="none" fill-opacity=".5">
            <g class="logo__trace" :stroke-width="strokeWidth">
                <path d="m58.128 114.12c65.13-92.727 204.64-91.46 270.89-79.359 181.55 33.16 195.19 182.89 92.317 231.26"/>
                <path d="m408.41 287.31c0.31355 41.613-13.349 52.65-63.14 59.087m50.501-66.758c-22.245 8.9941-32.078 12.891-57.404 16.091"/>
                <path d="m45.685 140.97c-11.7 22.84-31.021 103.84 30.969 142.64m23.893 13.73c34.682 14.492 97.426 18.602 124.56 13.667m15.186 9.7605s6.4626 27.161 28.236 40.106c14.421 8.5962 15.884 18.098 19.79 55.923"/>
                <path d="m332.66 363.14 14.633 74.677"/>
            </g>
            <g class="logo__arc" stroke-width="20">
                <path d="m303.51 224.26c81.105-46.863 65.803-98.391 58.493-135.33"/>
                <path d="m412.18 117.85c55.503 35.343 41.821 114.2-49.313 126.62"/>
                <path d="m230.43 82.038c-44.476 33.494-32.187 130.33-15.69 180.24"/>
                <path d="m303.51 76.901c-22.862-9.264-38.984-10.531-70.358 1.101"/>
                <path d="m155.91 264.11c-32.78 1.019-69.487-1.7727-80.167-42.543s10.247-111.35 86.321-133.93"/>
                <path d="m155.54 178.04c-24.46 8.68-55.321 11.825-78.191 4.2752"/>
            </g>
        </g>
        <g class="logo__node" :stroke-width="strokeWidth">
            <circle cx="51.448" cy="127.48" r="15" fill-opacity=".5"/>
            <circle cx="162.6" cy="86.189" r="15"/>
            <circle cx="231" cy="79.791" r="15"/>
            <circle cx="156.22" cy="264.22" r="15"/>
            <circle cx="216.63" cy="262.61" r="15"/>
            <circle cx="410.91" cy="116.26" r="15"/>
            <circle cx="362.65" cy="88.309" r="15"/>
            <circle cx="360.88" cy="243.99" r="15"/>
            <g fill-opacity=".5">
                <circle cx="408.07" cy="271.67" r="15"/>
                <circle cx="322.36" cy="296.27" r="15"/>
                <circle cx="330.2" cy="347.72" r="15"/>
                <circle cx="350.37" cy="451.94" r="15"/>
                <circle cx="290.56" cy="430.17" r="15"/>
                <circle cx="87.593" cy="289.01" r="15"/>
                <circle cx="237.91" cy="306.23" r="15"/>
            </g>
            <circle cx="291.48" cy="231.76" r="15"/>
            <circle cx="155.34" cy="176.6" r="15"/>
        </g>
        <path class="logo__arc" d="m240.65 173.25c51.651 11.245 114.53-49.846 63.724-95.891" fill="none" fill-opacity=".5" stroke-width="20"/>
        <circle class="logo__node" cx="303.52" cy="77.242" r="15" :stroke-width="strokeWidth"/>
    </svg>
</template>

<style scoped>
.logo {
    block-size: var(--logo-size, 1.5em);
    display: block;
    inline-size: var(--logo-size, 1.5em);
}

/* Traces and node outlines ride the inherited text colour. */
.logo__trace,
.logo__node {
    stroke: currentColor;
}

/*
 * The two blues are semantic rather than fixed, so they swap sides of the scale with
 * the colour mode: on-quiet resolves to brand-40 in light mode and brand-60 in dark,
 * border-loud the reverse. In light mode that lands within a few percent of the mark's
 * original #0057af and #007fff; in dark mode the arcs lighten and the nodes darken,
 * each staying the same distance from the surface it sits on.
 */
.logo__arc {
    stroke: var(--wa-color-brand-on-quiet);
}

.logo__node {
    fill: var(--wa-color-brand-border-loud);
}
</style>
