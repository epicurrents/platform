import { http } from '#lib/http'

export interface PushSubscriptionPayload {
    endpoint: string
    p256dh: string
    auth: string
}

export async function getVapidPublicKey(): Promise<string | null> {
    try {
        const response = await http.get<{ vapid_public_key: string }>('/api/v1/notifications/vapid-public-key')
        return response.data.vapid_public_key || null
    } catch {
        return null
    }
}

export async function saveSubscription(payload: PushSubscriptionPayload): Promise<void> {
    await http.post('/api/v1/notifications/subscribe', payload)
}

export async function removeSubscription(endpoint: string): Promise<void> {
    await http.delete('/api/v1/notifications/subscribe', { data: { endpoint } })
}
