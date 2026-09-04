import { getVapidPublicKey, removeSubscription, saveSubscription } from '#api/notifications'

/** Decode a base64url string into a Uint8Array for use as applicationServerKey. */
function urlBase64ToUint8Array(base64: string): Uint8Array {
    const padding = '='.repeat((4 - (base64.length % 4)) % 4)
    const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
    const raw = atob(normalized)
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)))
}

/** Return true when the browser supports service workers and the Push API. */
export function isPushSupported(): boolean {
    return (
        typeof window !== 'undefined' &&
        'serviceWorker' in navigator &&
        'PushManager' in window &&
        'Notification' in window
    )
}

/**
 * Request notification permission, register the push service worker, subscribe
 * via the Push API, and persist the subscription to the backend.
 *
 * Safe to call multiple times — if the browser already holds a subscription for
 * this SW the existing one is re-sent to the backend (handles key rotation and
 * missed saves on earlier logins).
 *
 * Silently returns without throwing on any error so callers never need to handle
 * push-related exceptions.
 */
export async function subscribeToPush(): Promise<void> {
    if (!isPushSupported()) return

    try {
        const permission = await Notification.requestPermission()
        if (permission !== 'granted') return

        const vapidPublicKey = await getVapidPublicKey()
        if (!vapidPublicKey) return

        // Subscribe against the single app service worker registered in main.ts
        // (precache + push). No separate registration here — that is what used to
        // create a second worker racing for the root scope.
        const registration = await navigator.serviceWorker.ready

        const subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(vapidPublicKey).buffer as ArrayBuffer,
        })

        const json = subscription.toJSON()
        await saveSubscription({
            endpoint: json.endpoint!,
            p256dh: json.keys?.p256dh ?? '',
            auth: json.keys?.auth ?? '',
        })
    } catch (err) {
        // Push is best-effort — a failure here must never break the app.
        if (import.meta.env.DEV) console.warn('[webpush] subscribeToPush failed:', err)
    }
}

/**
 * Unsubscribe the current push subscription and remove it from the backend.
 * Called on logout so stale endpoints are cleaned up immediately.
 */
export async function unsubscribeFromPush(): Promise<void> {
    if (!isPushSupported()) return

    try {
        const registration = await navigator.serviceWorker.getRegistration()
        if (!registration) return

        const subscription = await registration.pushManager.getSubscription()
        if (!subscription) return

        await removeSubscription(subscription.endpoint)
        await subscription.unsubscribe()
    } catch (err) {
        if (import.meta.env.DEV) console.warn('[webpush] unsubscribeFromPush failed:', err)
    }
}
