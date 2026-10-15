import { useCallback, useEffect, useState } from 'react'
import { api, fmt, getSession, signIn, type Product, type Role, type Status } from './api'
import { AppContext, LogoMark, ROLE_LABEL, RoleGate } from './ui'
import Home from './pages/Home'
import NewClient from './pages/NewClient'
import Session from './pages/Session'
import Oversight from './pages/Oversight'
import Regulator from './pages/Regulator'

function useRoute() {
  const read = () => window.location.hash.replace(/^#\/?/, '')
  const [route, setRoute] = useState(read)
  useEffect(() => {
    const on = () => { setRoute(read()); window.scrollTo(0, 0) }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}

export default function App() {
  const route = useRoute()
  const [status, setStatus] = useState<Status | null>(null)
  const [catalog, setCatalog] = useState<Record<string, Product>>({})
  const [offline, setOffline] = useState(false)
  const [open, setOpen] = useState(false)
  const [role, setRole] = useState<Role | null>(() => getSession()?.role ?? null)
  const switchRole = useCallback(async (r: Role) => {
    const s = await signIn(r)
    setRole(s.role)
  }, [])
  useEffect(() => { if (!getSession()) switchRole('client').catch(() => setOffline(true)) }, [switchRole])

  const refreshStatus = useCallback(async () => {
    try { setStatus(await api.status()); setOffline(false) } catch { setOffline(true) }
  }, [])
  const reloadCatalog = useCallback(async () => {
    const list = await api.catalog()
    setCatalog(Object.fromEntries(list.map(p => [p.id, p])))
  }, [])

  useEffect(() => {
    refreshStatus()
    reloadCatalog().catch(() => setOffline(true))
    const t = setInterval(refreshStatus, 6000)
    return () => clearInterval(t)
  }, [refreshStatus, reloadCatalog])

  const [section, arg] = route.split('/')
  const area = section === 'oversight' ? 'oversight' : section === 'regulator' ? 'regulator' : 'advisor'
  const fisco = status?.chain.kind.startsWith('FISCO')
  const scripted = status?.model.startsWith('demo')
  const live = !offline && !!status

  return (
    <AppContext.Provider value={{ catalog, status, role, switchRole, reloadCatalog, refreshStatus }}>
      <header className="top">
        <div className="top-in">
          <a className="logo" href="#/"><LogoMark /> Signet</a>
          <nav className="nav">
            <a href="#/" aria-current={area === 'advisor' ? 'page' : undefined}>Advisor</a>
            <a href="#/oversight" aria-current={area === 'oversight' ? 'page' : undefined}>
              Bank oversight{role === 'bank' && status && status.compliance_queue > 0 ? ` · ${status.compliance_queue}` : ''}
            </a>
            <a href="#/regulator" aria-current={area === 'regulator' ? 'page' : undefined}>Regulator</a>
          </nav>
          <label className="role-pick">
            <span className="tiny faint">Viewing as</span>
            <select value={role ?? ''} onChange={e => switchRole(e.target.value as Role)} aria-label="Viewing as">
              {(['client', 'bank', 'regulator'] as Role[]).map(r => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
            </select>
          </label>
          <div className="live">
            <button className="live-btn" onClick={() => setOpen(o => !o)} aria-expanded={open}>
              <span className={`pulse ${live && fisco && !scripted ? '' : 'off'}`} />
              {offline ? 'Backend offline' : live ? (fisco && !scripted ? 'System live' : 'Offline demo mode') : 'Connecting…'}
            </button>
            {open && status && (
              <div className="popover" onMouseLeave={() => setOpen(false)}>
                <h3>Running locally on this machine</h3>
                {(scripted || !fisco) && <p className="tiny muted" style={{ margin: '0 0 8px' }}>
                  Offline demo mode: {scripted ? 'a scripted stand-in plays the AI (it deliberately makes the classic mistakes so you can watch them get blocked)' : ''}{scripted && !fisco ? ', and ' : ''}{!fisco ? 'certificates are anchored on an in-process EVM instead of FISCO BCOS' : ''}. The verifier, solver, certificates and gateway are the real ones.
                </p>}
                <div className="spread"><span className="muted">AI advisor</span><b>{scripted ? 'scripted demo (no LLM)' : status.model.replace('ollama:', '')}</b></div>
                <div className="spread"><span className="muted">Compliance sentinel</span><b>{status.sentinel ?? 'not loaded'}</b></div>
                <div className="spread"><span className="muted">Blockchain</span><b>{fisco ? `FISCO BCOS · chain ${status.chain.chain_id}` : 'in-process (fallback)'}</b></div>
                {status.chain.note && <p className="tiny faint" style={{ margin: '2px 0 6px' }}>{status.chain.note}</p>}
                <div className="spread"><span className="muted">Registry contract</span><span className="mono">{fmt.short(status.chain.contract, 6)}</span></div>
                <div className="spread"><span className="muted">Evidence log</span><b>{status.ledger_ok ? 'intact' : 'BROKEN'} · {status.ledger_events} events</b></div>
                <div className="spread"><span className="muted">Certificates issued</span><b className="num">{status.certificates.toLocaleString()}</b></div>
                <div className="divider" />
                {role === 'bank' && <button className="btn sm" onClick={async () => {
                  if (!window.confirm('Reset all demo data? Clients you created, recommendations, cases, messages, orders and product ratings will be cleared. The evidence log and blockchain keep their history.')) return
                  await api.resetDemo()
                  window.location.hash = '/'
                  window.location.reload()
                }}>Reset demo data</button>}
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="page">
        {role && <>
          {section === '' && <RoleGate need={['client', 'bank']}><Home /></RoleGate>}
          {section === 'new' && <RoleGate need={['client', 'bank']}><NewClient /></RoleGate>}
          {section === 'client' && arg && <RoleGate need={['client', 'bank']}><Session clientId={arg} key={`${arg}-${role}`} /></RoleGate>}
          {section === 'oversight' && <RoleGate need={['bank']}><Oversight key={role} /></RoleGate>}
          {section === 'regulator' && <RoleGate need={['regulator']}><Regulator key={role} /></RoleGate>}
        </>}
      </main>
    </AppContext.Provider>
  )
}
