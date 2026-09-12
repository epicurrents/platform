<template>
    <TransitionGroup
        aria-atomic="false"
        aria-live="polite"
        class="toast-stack"
        name="toast"
        tag="div"
    >
        <div
            v-for="toast in toasts"
            :key="toast.id"
            class="toast"
            @click="dismissToast(toast.id)"
            @mouseenter="pauseToast(toast.id)"
            @mouseleave="resumeToast(toast.id)"
        >
            <wa-callout role="status" :variant="toast.variant">
                <app-icon :library="iconLibrary" :name="iconFor(toast.variant)" slot="icon"></app-icon>
                <div class="toast-body">
                    <div v-if="toast.topic" class="toast-topic" :title="toast.topic">
                        {{ toast.topic }}
                    </div>
                    <div class="toast-message" :class="{ 'has-topic': toast.topic }" :title="toast.message">
                        {{ toast.message }}
                    </div>
                    <a
                        v-if="toast.action"
                        class="toast-action"
                        href="#"
                        role="button"
                        @click.stop.prevent="runAction(toast)"
                    >{{ toast.action.label }}</a>
                </div>
            </wa-callout>
            <div
                v-if="toast.duration > 0"
                class="toast-progress"
                :style="{ animationDuration: toast.duration + 'ms' }"
            ></div>
        </div>
    </TransitionGroup>
</template>

<script setup lang="ts">
/**
 * Toast stack, rendering the reactive stack from `#lib/toast`. The embedded viewer announces through
 * `window.__EPICURRENTS__.announce`, which routes into that same stack, so viewer callouts and platform
 * ones share one surface and one set of dismissal rules.
 * @package    epicurrents-platform
 */

import { onMounted } from 'vue'
import { dismissToast, pauseToast, resumeToast, showToast, toasts } from '../lib/toast'
import type { Toast, ToastVariant } from '../lib/toast'
import AppIcon from './AppIcon.vue'

// Run a toast's action link, then dismiss the toast. `@click.stop` on the link keeps this from also firing the
// toast's outer click-to-dismiss, so the action always runs.
function runAction (toast: Toast): void {
    toast.action?.run()
    dismissToast(toast.id)
}

const props = withDefaults(
    defineProps<{
        /**
         * WebAwesome icon library to resolve the toast icons from. Defaults to the viewer's `epicurrents` library; a
         * host embedding this stack (e.g. the platform) passes its own library so the icons resolve there too.
         */
        iconLibrary?: string
        /**
         * Fire one toast of every variant (plus a topic example) on mount, to visually check the stack. Off by default;
         * enable only while testing.
         */
        testBattery?: boolean
    }>(),
    { iconLibrary: 'epicurrents', testBattery: false },
)

// Current WebAwesome icon names, present in both the viewer's and the platform's icon libraries (avoiding the
// stale Shoelace-era aliases the viewer's map still carries).
function iconFor (variant: ToastVariant): string {
    switch (variant) {
        case 'success': return 'circle-check'
        case 'warning': return 'triangle-exclamation'
        case 'danger':  return 'circle-exclamation'
        case 'neutral': return 'circle-info'
        case 'brand':   return 'circle-info'
    }
}

onMounted(() => {
    if (!props.testBattery) {
        return
    }
    // Persistent set (`duration: 0`) — stays until clicked so every variant, the topic line, and descenders
    // (Danger toast has a "g") can be compared side by side.
    showToast('Brand — an unprompted notification', 'brand', 0)
    showToast('Success — the awaited task completed', 'success', 0)
    showToast('Neutral — a routine confirmation', 'neutral', 0)
    showToast('Warning — something needs attention', 'warning', 0)
    showToast('Danger — an operation failed (typography: gjpy)', 'danger', 0)
    showToast(['Topic line (bold)', 'And the message body on the line below it'], 'neutral', 0)
    // Timed set — auto-derived duration, to check the countdown bar, hover-to-pause, and auto-dismiss.
    showToast('Brand — auto-dismisses on a timer', 'brand')
    showToast('Success — auto-dismisses on a timer', 'success')
    showToast('Neutral — auto-dismisses on a timer', 'neutral')
    showToast('Warning — auto-dismisses on a timer', 'warning')
    showToast('Danger — auto-dismisses on a timer', 'danger')
})
</script>

<style scoped>
.toast-stack {
    bottom: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-width: min(32rem, calc(100vw - 2rem));
    pointer-events: none;
    position: absolute;
    right: 1rem;
    z-index: 9999;
}

.toast {
    border-radius: var(--wa-panel-border-radius);
    cursor: pointer;
    overflow: hidden;
    pointer-events: auto;
    position: relative;
    /* Tighten the icon-to-text gap inside the callout (the callout defaults to ~1.5em). */
    --wa-form-control-padding-inline: var(--wa-space-s);
}

/* A comfortable line-height so glyphs sit centred in their line box (not crowded to the top) and descenders
   (g, y, p) are not clipped by the overflow:hidden clamp. */
.toast-body {
    line-height: 1.4;
}

.toast-topic {
    font-weight: var(--wa-font-weight-bold, 700);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.toast-message {
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    display: -webkit-box;
    line-clamp: 2;
    overflow: hidden;
}

/* With a topic line above, keep the message to a single line so a toast stays ~two lines. */
.toast-message.has-topic {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* Action link (e.g. "Reload") — an inline call-to-action under the message. Underlined and bold so it reads as
   the actionable element within the otherwise click-to-dismiss toast. */
.toast-action {
    color: inherit;
    cursor: pointer;
    display: inline-block;
    font-weight: var(--wa-font-weight-bold, 700);
    margin-top: 0.25rem;
    text-decoration: underline;
}

/* Countdown bar at the bottom edge; pauses on hover in sync with the JS pause/resume timer. */
.toast-progress {
    animation-fill-mode: forwards;
    animation-name: toast-countdown;
    animation-timing-function: linear;
    background-color: var(--wa-color-text-normal);
    bottom: 0;
    height: 2px;
    left: 0;
    opacity: 0.2;
    position: absolute;
    right: 0;
    transform-origin: left;
}

.toast:hover .toast-progress {
    animation-play-state: paused;
}

@keyframes toast-countdown {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
}

.toast-enter-active,
.toast-leave-active,
.toast-move {
    transition: opacity 0.2s ease, transform 0.2s ease;
}

.toast-enter-from,
.toast-leave-to {
    opacity: 0;
    transform: translateX(20px);
}

.toast-leave-active {
    position: absolute;
    right: 0;
    width: 100%;
}
</style>
