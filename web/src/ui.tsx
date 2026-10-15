import { createContext, useContext, type ReactNode } from 'react'
import { fmt, type Product, type Role, type Scope, type Status } from './api'
import { RISK_NAME } from './plain'

export const AppContext = createContext<{
  catalog: Record<string, Product>
  status: Status | null
  role: Role | null
  switchRole: (r: Role) => Promise<void>
  reloadCatalog: () => Promise<void>
  refreshStatus: () => Promise<void>
}>({ catalog: {}, status: null, role: null, switchRole: async () => {}, reloadCatalog: async () => {}, refreshStatus: async () => {} })

export const useApp = () => useContext(AppContext)

export function navigate(path: string) {
  window.location.hash = path
}

export function Risk({ level, prefix = 'R' }: { level: number; prefix?: 'R' | 'C' }) {
  return <span className={`risk l${level}`} title={prefix === 'R' ? RISK_NAME[level] : undefined}>{prefix}{level}</span>
}

export function ProductRow({ id, amount }: { id: string; amount?: number }) {
  const { catalog } = useApp()
  const p = catalog[id]
  return (
    <div className="prod">
      {p ? <Risk level={p.effective_risk} /> : <span className="risk">?</span>}
      <span className="prod-name">
        {p?.name ?? id}
        {p && <small>{RISK_NAME[p.effective_risk]}{p.market === 'HK' ? ' · Hong Kong fund' : ''}</small>}
      </span>
      {amount !== undefined && <span className="prod-amt">{fmt.rmb(amount)}</span>}
    </div>
  )
}

/** Highlights each excerpt inside the text. */
export function Highlight({ text, marks }: { text: string; marks: string[] }) {
  const clean = marks.map(m => m.trim()).filter(m => m.length > 3 && text.includes(m))
  if (!clean.length) return <>{text}</>
  const parts: ReactNode[] = []
  let rest = text
  let key = 0
  while (rest) {
    let best = -1
    let found = ''
    for (const m of clean) {
      const i = rest.indexOf(m)
      if (i >= 0 && (best < 0 || i < best)) { best = i; found = m }
    }
    if (best < 0) { parts.push(rest); break }
    parts.push(rest.slice(0, best))
    parts.push(<mark key={key++}>{found}</mark>)
    rest = rest.slice(best + found.length)
  }
  return <>{parts}</>
}

const COLORS = ['#1f4bd8', '#0f7a55', '#a15c00', '#7a3fd0', '#c2410c', '#0e7490']

export function Avatar({ name, size = 44 }: { name: string; size?: number }) {
  const initials = name.split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase()
  const color = COLORS[[...name].reduce((a, ch) => a + ch.charCodeAt(0), 0) % COLORS.length]
  return <span className="avatar" style={{ background: color, width: size, height: size }}>{initials}</span>
}

export const Icon = {
  check: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>,
  x: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
  spark: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18" /></svg>,
  send: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6" /></svg>,
  user: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><circle cx="12" cy="8" r="4" /><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" /></svg>,
  chain: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><rect x="3" y="8" width="8" height="8" rx="2" /><rect x="13" y="8" width="8" height="8" rx="2" /><path d="M11 12h2" /></svg>,
  back: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M15 6l-6 6 6 6" /></svg>,
}

export function LogoMark() {
  return (
    <span className="logo-mark" aria-hidden>
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
    </span>
  )
}

/** What a certificate does and doesn't establish (signed into the certificate). */
export function ScopeBlock({ scope }: { scope: Scope }) {
  return (
    <div className="scope">
      <div className="scope-row"><span className="ok-ic">{Icon.check}</span>
        <div><b>Proven by the verifier</b><span>{scope.verified}</span></div></div>
      <div className="scope-row"><span className="mid-ic">~</span>
        <div><b>Screened, not proven</b><span>{scope.pattern_checked}</span></div></div>
      <div className="scope-row"><span className="no-ic">{Icon.x}</span>
        <div><b>Not checked</b><span>{scope.not_checked.join(' · ')}</span></div></div>
    </div>
  )
}

export const ROLE_LABEL: Record<Role, string> = { client: 'Client', bank: 'Bank staff', regulator: 'Regulator' }
const ROLE_GROUP: Record<Role, string> = { client: 'clients', bank: 'bank staff', regulator: 'the regulator' }
const ROLE_AS: Record<Role, string> = { client: 'a client', bank: 'bank staff', regulator: 'the regulator' }

/** Shown when the signed-in role isn't allowed in an area. The API enforces the same rule. */
export function RoleGate({ need, children }: { need: Role[]; children: ReactNode }) {
  const { role, switchRole } = useApp()
  if (role && need.includes(role)) return <>{children}</>
  return (
    <div className="card stack" style={{ maxWidth: 560, margin: '40px auto', gap: 14 }}>
      <h2>This area is for {need.map(r => ROLE_GROUP[r]).join(' or ')}</h2>
      <p className="muted">You’re signed in as {role ? ROLE_AS[role] : 'nobody'}. The server checks the role on every request,
        so this isn’t just hidden: {role ? ROLE_AS[role] : 'a visitor'} can’t call these actions at all.</p>
      <div className="row wrap">
        {need.map(r => <button key={r} className="btn primary" onClick={() => switchRole(r)}>Sign in as {ROLE_AS[r]}</button>)}
      </div>
      <p className="tiny faint">Demo sign-in: no passwords. In production the role would come from the bank’s identity system.</p>
    </div>
  )
}
