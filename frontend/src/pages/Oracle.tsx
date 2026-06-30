import { useRef } from 'react'
import { useAuth } from '../contexts/AuthContext'

export default function Oracle() {
  const { user, session, signOut } = useAuth()
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const sendAuth = () => {
    iframeRef.current?.contentWindow?.postMessage(
      { type: 'AUTH', token: session?.access_token, userId: user?.id },
      window.location.origin
    )
  }

  return (
    <div className="oracle-page">
      <div className="oracle-topbar">
        <span className="oracle-user">{user?.email}</span>
        <button className="btn-signout" onClick={signOut}>Sign Out</button>
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
