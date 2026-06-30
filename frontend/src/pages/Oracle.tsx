import { useAuth } from '../contexts/AuthContext'

export default function Oracle() {
  const { user, signOut } = useAuth()

  return (
    <div className="oracle-page">
      <div className="oracle-topbar">
        <span className="oracle-user">{user?.email}</span>
        <button className="btn-signout" onClick={signOut}>Sign Out</button>
      </div>
      <iframe
        src="/oracle.html"
        title="Cosmic Lottery Oracle"
        className="oracle-frame"
      />
    </div>
  )
}
