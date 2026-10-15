import { useCallback, useEffect, useState } from 'react'
import { api, fmt, type Case, type LedgerEvent } from '../api'
import { RISK_NAME } from '../plain'
import { navigate, Risk, useApp } from '../ui'

type Rerate = Awaited<ReturnType<typeof api.rerate>>
type Names = (id: string) => string

const EVENT_TEXT: Record<string, (d: Record<string, unknown>, name: Names) => string> = {
  client_created: d => `New client assessed at C${d.level}`,
  assessment: d => `Risk questionnaire scored: C${d.level}`,
  assessment_refused: () => 'Questionnaire refused: retaken too often',
  advice: d => `Advice given: ${d.drafts} AI draft(s), ${d.first_pass_ok ? 'passed first time' : 'first draft stopped'}`,
  profile_integrity: () => 'Client’s words contradicted their questionnaire',
  confirmed: d => `Client confirmed; recorded on-chain (tx ${fmt.short(String(d.tx_hash), 5)})`,
  account_opened: () => 'Sandbox account opened',
  order: (d, name) => `Order: ${name(String(d.product_id))} ${fmt.rmb(Number(d.amount))} (${String(d.status).replaceAll('_', ' ')})`,
  execution_done: d => `Orders executed (${(d.orders as string[]).length})`,
  execution_blocked: d => `Order refused: ${(d.reasons as string[])[0]}`,
  product_rerated: (d, name) => `${name(String(d.product_id))} re-rated R${d.from} → R${d.to}`,
  regulator_flag: () => 'Regulator flagged a recommendation on-chain',
  rule_change_simulated: d => `Rule change simulated: ${d.affected} affected`,
  synthetic_batch: d => `${Number(d.count).toLocaleString()} demo certificates recorded in one transaction`,
  clock_advanced: d => `Demo clock moved forward ${d.hours}h`,
  permission_denied: d => `Refused: ${d.actor} tried ${d.tool}`,
  case_opened: () => 'Review case opened',
  case_resolved: d => `Case resolved: ${String(d.action).replaceAll('_', ' ')}. “${d.reason}”`,
  client_notified: d => `Client notified: ${d.title}`,
}

const TYPE_TAG: Record<string, { label: string; cls: string }> = {
  profile_capped: { label: 'Profile', cls: 'warn' },
  profile_change: { label: 'Profile', cls: 'warn' },
  rerating: { label: 'Re-rating', cls: 'brand' },
  execution_blocked: { label: 'Order', cls: 'bad' },
  regulator_flag: { label: 'Regulator', cls: 'bad' },
}

export default function Oversight() {
  const { catalog, reloadCatalog, refreshStatus } = useApp()
  const [tab, setTab] = useState<'open' | 'resolved'>('open')
  const [cases, setCases] = useState<Case[]>([])
  const [log, setLog] = useState<{ ok: boolean; events: LedgerEvent[] } | null>(null)
  const [pid, setPid] = useState('CN-FUND-DIV')
  const [risk, setRisk] = useState(4)
  const [res, setRes] = useState<Rerate | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const [c, l] = await Promise.all([api.cases(tab), api.ledger()])
    setCases(c); setLog(l)
  }, [tab])
  useEffect(() => { load() }, [load])

  async function rerate() {
    setBusy(true)
    try { setRes(await api.rerate(pid, risk)); await Promise.all([reloadCatalog(), load(), refreshStatus()]) } finally { setBusy(false) }
  }
  const p = catalog[pid]

  return (
    <div>
      <div className="title-block">
        <h1 style={{ fontSize: 34 }}>Bank oversight</h1>
        <p>The AI handles routine advice. Anything unusual lands here automatically, a person decides, and every decision is written to the evidence log.</p>
      </div>
      <div className="cols">
        <div className="card stack" style={{ gap: 6 }}>
          <div className="spread">
            <h2>Review cases</h2>
            <div className="app-tabs" style={{ marginTop: 0, width: 220 }}>
              <button aria-pressed={tab === 'open'} onClick={() => setTab('open')}>Open</button>
              <button aria-pressed={tab === 'resolved'} onClick={() => setTab('resolved')}>Resolved</button>
            </div>
          </div>
          {cases.length === 0 && (
            <div className="empty">
              {tab === 'open'
                ? <>Nothing waiting. Cases appear when a client’s words contradict their questionnaire, a product they own is re-rated, an order is refused, or the regulator flags a sale.</>
                : 'No resolved cases yet.'}
            </div>
          )}
          {cases.map(c => <CaseCard key={c.id} c={c} onDone={() => { load(); refreshStatus() }} />)}
        </div>

        <div className="card stack" style={{ gap: 16, alignSelf: 'start' }}>
          <div>
            <h2>A product’s risk rating changes</h2>
            <p className="muted small" style={{ marginTop: 4 }}>
              Everyone holding the product, and every pending order, is re-checked straight away. Affected clients are told automatically (NFRA Art. 22) and a case opens here.
            </p>
          </div>
          <div className="field"><label>Product</label>
            <select value={pid} onChange={e => setPid(e.target.value)}>
              {Object.values(catalog).map(x => <option key={x.id} value={x.id}>{x.name} · now R{x.effective_risk}</option>)}
            </select></div>
          <div className="field"><label>New rating</label>
            <div className="choices" style={{ marginTop: 0 }}>
              {[1, 2, 3, 4, 5].map(n => <button key={n} className="choice" aria-pressed={risk === n} onClick={() => setRisk(n)}>R{n} · {RISK_NAME[n]}</button>)}
            </div></div>
          <button className="btn primary" style={{ alignSelf: 'flex-start' }} disabled={busy} onClick={rerate}>Apply new rating</button>
          {p && <p className="small muted">Current: <Risk level={p.effective_risk} /> (the higher of the issuer’s and distributor’s ratings applies)</p>}
          {res && (
            <div className={res.affected.length ? 'notice' : 'receipt'}>
              <b>{catalog[res.product_id]?.name}: R{res.from} → R{res.to}</b>
              {res.affected.length === 0
                ? <span className="small">Nobody holds it or has a pending order above their level, so no client is affected. Tip: buy it for a client first, then raise the rating.</span>
                : res.affected.map(a => (
                  <span className="small" key={a.client_id}>
                    {a.name} (C{a.level}) {a.holds ? 'holds it' : 'has a pending order'}. They’ve been messaged and a case is open.{' '}
                    <a href={`#/client/${a.client_id}`}>See their phone →</a>
                  </span>))}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 24 }}>
        <div className="spread" style={{ marginBottom: 8 }}>
          <div><h2>Evidence log</h2><p className="small muted" style={{ marginTop: 4 }}>Every event is chained to the one before it, so editing any past entry breaks the chain (NFRA Art. 17).</p></div>
          {log && <span className={`tag ${log.ok ? 'good' : 'bad'}`}>{log.ok ? 'Chain intact' : 'Chain broken'}</span>}
        </div>
        <div className="log">
          {log?.events.slice().reverse().slice(0, 40).map(e => (
            <div className="log-row" key={e.seq}>
              <span className="mono faint">{e.ts.slice(11, 19)}</span>
              <span>{(EVENT_TEXT[e.type] ?? (() => e.type.replaceAll('_', ' ')))(e.data, id => catalog[id]?.name ?? id)}</span>
              <span className="mono faint">{e.hash.slice(0, 8)}</span>
            </div>
          ))}
          {log && log.events.length === 0 && <div className="empty">No events yet.</div>}
        </div>
      </div>
    </div>
  )
}

function CaseCard({ c, onDone }: { c: Case; onDone: () => void }) {
  const [action, setAction] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const tag = TYPE_TAG[c.type] ?? { label: c.type, cls: '' }

  async function resolve() {
    if (!action) return
    setBusy(true); setErr(null)
    try { await api.resolveCase(c.id, action, reason); onDone() } catch (e) { setErr(String(e).replace('Error: ', '')) } finally { setBusy(false) }
  }

  return (
    <div className="case" style={{ flexDirection: 'column', gap: 10 }}>
      <div className="row" style={{ alignItems: 'flex-start', gap: 12 }}>
        <span className={`tag ${tag.cls}`}>{tag.label}</span>
        <div className="stack grow" style={{ gap: 3 }}>
          <b>{c.title}</b>
          <span className="small muted">
            <a href={`#/client/${c.client_id}`} onClick={e => { e.preventDefault(); navigate(`/client/${c.client_id}`) }}>{c.name}</a> · {c.at.slice(0, 16).replace('T', ' ')}
          </span>
          <span className="small">{c.summary}</span>
          {c.details.map(d => <span key={d} className="small muted" style={{ fontStyle: 'italic' }}>{d}</span>)}
        </div>
      </div>

      {c.status === 'resolved' && c.resolution && (
        <div className="receipt small" style={{ gap: 4 }}>
          <b>{c.resolution.label}</b>
          <span>“{c.resolution.reason}”</span>
          <span className="muted">{c.resolution.effect}</span>
        </div>
      )}

      {c.status === 'open' && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="choices" style={{ marginTop: 0 }}>
            {Object.entries(c.actions).map(([k, label]) => (
              <button key={k} className="choice" aria-pressed={action === k} onClick={() => setAction(k)}>{label}</button>
            ))}
          </div>
          {action && (
            <div className="stack rise" style={{ gap: 8 }}>
              <textarea rows={2} className="input" placeholder="Reason (goes into the evidence log)" value={reason} onChange={e => setReason(e.target.value)} />
              {err && <span className="small" style={{ color: 'var(--bad)' }}>{err}</span>}
              <button className="btn primary sm" style={{ alignSelf: 'flex-start' }} disabled={busy || reason.trim().length < 8} onClick={resolve}>
                {busy && <span className="spinner" />} Confirm decision
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
