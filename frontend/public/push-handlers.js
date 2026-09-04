/**
 * push-handlers.js — web-push logic for the single app service worker.
 *
 * This is NOT registered on its own. The vite-plugin-pwa generated service
 * worker pulls it in with `importScripts('push-handlers.js')` (see
 * `workbox.importScripts` in vite.config.ts), so one worker owns the root scope
 * and does both precaching and push. It lives in public/ so it is copied to the
 * site root unhashed at the stable path the generated worker imports.
 *
 * (Replaces the former standalone /push-sw.js; main.ts unregisters any lingering
 * push-sw.js registration on startup so old clients migrate to the single worker.)
 */

self.addEventListener('push', (event) => {
    let data = {}
    if (event.data) {
        try {
            data = event.data.json()
        } catch {
            data = { title: 'Epicurrents', body: event.data.text() }
        }
    }

    const title = data.title || 'Epicurrents'
    const options = {
        body: data.body || '',
        icon: '/pwa-192x192.png',
        badge: '/pwa-192x192.png',
        data,
    }

    event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
    event.notification.close()
    const url = event.notification.data?.url || '/'

    event.waitUntil(
        clients
            .matchAll({ type: 'window', includeUncontrolled: true })
            .then((clientList) => {
                for (const client of clientList) {
                    if ('focus' in client) return client.focus()
                }
                if (clients.openWindow) return clients.openWindow(url)
            })
    )
})
