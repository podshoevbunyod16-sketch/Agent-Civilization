import { useEffect, useRef, useState } from 'react'

/** Загрузка данных с состояниями loading/error и возможностью повторить. */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): { data: T | null; loading: boolean; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Не удалось загрузить данные')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  return { data, loading, error, reload: () => setNonce((value) => value + 1) }
}

/** Периодическое обновление (для мониторинга узлов и задач). */
export function usePolling(callback: () => void, intervalMs: number, enabled = true): void {
  const saved = useRef(callback)
  saved.current = callback
  useEffect(() => {
    if (!enabled) return
    const timer = window.setInterval(() => saved.current(), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs, enabled])
}
