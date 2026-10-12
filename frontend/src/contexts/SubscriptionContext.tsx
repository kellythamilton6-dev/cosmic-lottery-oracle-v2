import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import type { ReactNode } from 'react'
import { useAuth } from './AuthContext'

const API = import.meta.env.VITE_ORACLE_API_URL as string

interface SubscriptionState {
  tier: 'free' | 'pro'
  status: string
  loading: boolean
  refresh: () => Promise<void>
  startCheckout: () => Promise<void>
  openPortal: () => Promise<void>
}

const SubscriptionContext = createContext<SubscriptionState | null>(null)

export function SubscriptionProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth()
  const [tier, setTier] = useState<'free' | 'pro'>('free')
  const [status, setStatus] = useState('inactive')
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    if (!session?.access_token) { setLoading(false); return }
    try {
      const res = await fetch(`${API}/subscription/status`, {
        headers: { Authorization: `Bearer ${session.access_token}` }
      })
      const data = await res.json()
      setTier(data.tier === 'pro' ? 'pro' : 'free')
      setStatus(data.status)
    } catch {
      // silently keep free tier on network error
    } finally {
      setLoading(false)
    }
  }, [session?.access_token])

  useEffect(() => { refresh() }, [refresh])

  const startCheckout = async () => {
    if (!session?.access_token) return
    const res = await fetch(`${API}/stripe/create-checkout-session`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${session.access_token}` }
    })
    const data = await res.json()
    if (data.url) window.location.href = data.url
  }

  const openPortal = async () => {
    if (!session?.access_token) return
    const res = await fetch(`${API}/stripe/create-portal-session`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${session.access_token}` }
    })
    const data = await res.json()
    if (data.url) window.location.href = data.url
  }

  return (
    <SubscriptionContext.Provider value={{ tier, status, loading, refresh, startCheckout, openPortal }}>
      {children}
    </SubscriptionContext.Provider>
  )
}

export const useSubscription = () => {
  const ctx = useContext(SubscriptionContext)
  if (!ctx) throw new Error('useSubscription must be used within SubscriptionProvider')
  return ctx
}
