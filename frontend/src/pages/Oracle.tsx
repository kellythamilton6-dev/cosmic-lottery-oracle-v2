import { useRef, useEffect } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useSubscription } from '../contexts/SubscriptionContext'

export default function Oracle() {
  const { user, session, signOut } = useAuth()
  const { tier, loading, startCheckout, openPortal, refresh } = useSubscription()
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const sendAuth = () => {
    iframeRef.current?.contentWindow?.postMessage(
      { type: 'AUTH', token: session?.access_token, userId: user?.id, tier },
      window.location.origin
    )
  }

  // Re-send auth+tier whenever tier resolves (avoids race where iframe loads before tier fetched)
  useEffect(() => {
    if (!loading) sendAuth()
  }, [loading, tier])

  // Listen for upgrade trigger from iframe
  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (e.data?.type === 'UPGRADE') startCheckout()
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [startCheckout])

  // Handle Stripe redirect back to app
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('upgrade') === 'success') {
      // Poll until subscription is active (webhook may be slightly delayed)
      let attempts = 0
      const poll = setInterval(async () => {
        await refresh()
        attempts++
        if (attempts >= 8) clearInterval(poll)
      }, 2000)
      window.history.replaceState({}, '', '/')
    }
  }, [])

  return (
    <div className="oracle-page">
      <div className="oracle-topbar">
        <span className="oracle-user">{user?.email}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {!loading && (
            tier === 'pro' ? (
              <button
                className="btn-pro"
                onClick={openPortal}
                title="Manage billing"
              >
                ✦ Pro
              </button>
            ) : (
              <button
                className="btn-upgrade"
                onClick={startCheckout}
              >
                ⚡ Upgrade to Pro
              </button>
            )
          )}
          <button className="btn-signout" onClick={signOut}>Sign Out</button>
        </div>
      </div>
      <iframe
        ref={iframeRef}
        src="/oracle.html"
        title="Cosmic Lottery Oracle"
        className="oracle-frame"
        onLoad={sendAuth}
      />
    </div>
  )
}
