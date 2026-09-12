<template>
    <wa-icon data-component="app-icon"
        :class="appearance ? `wa-${appearance}` : undefined"
        :label="label"
        :library="library"
        :name="name"
        :style="{
            transform: [
                rotate ? `rotate(${rotate})` : null,
                scale !== null ? `scale(${scale})` : null,
            ].filter(Boolean).join(' ') || undefined,
            opacity: empty ? '0' : '1',
        }"
        :variant="variant"
    ></wa-icon>
</template>

<script lang="ts">
import { defineComponent } from "vue"

/**
 * Icon component backed by Material Design Icons, served via the WebAwesome icon slot.
 * Accepts both FA kebab-case names (translated automatically) and Material snake_case names.
 * The `variant` prop maps to the Material icon style: "solid" → filled, all others → outlined.
 */
export default defineComponent({
    name: 'AppIcon',
    props: {
        /** Appearance style of the icon (maps to wa-${appearance} CSS class). */
        appearance: {
            type: String,
        },
        /** Display an empty space in place of the icon. */
        empty: {
            type: Boolean,
            default: false,
        },
        /** Accessible label for the icon. */
        label: {
            type: String,
        },
        /** WebAwesome icon library to resolve the icon from. Defaults to the app's `epicurrents` library. */
        library: {
            type: String,
            default: 'epicurrents',
        },
        /** Name of the icon to display. Accepts both FA kebab-case and Material snake_case names. */
        name: {
            type: String,
            required: true,
        },
        /** CSS transform rotation to apply to the icon (e.g., "90deg", "0.25turn"). */
        rotate: {
            type: String,
            default: null,
        },
        /** CSS transform scale to apply to the icon (e.g., 0.5, 1.25). */
        scale: {
            type: Number,
            default: null,
        },
        /** Icon style variant. "solid" maps to the Material filled set; all others use outlined. */
        variant: {
            type: String,
            default: 'regular',
        },
    },
})
</script>

<style scoped>
[data-component="app-icon"] {
    font-size: inherit;
}
</style>
