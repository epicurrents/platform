/**
 * Toast notification stack — re-exported from the viewer so the platform and the embedded viewer share a single
 * implementation. The viewer is the canonical source (the platform depends on it, not the other way around); see
 * `frontend/viewer/interface/src/lib/toast.ts`.
 */

export * from '#root/viewer/interface/src/lib/toast'
