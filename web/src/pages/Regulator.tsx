import { useCallback, useEffect, useState } from 'react'
import { api, fmt, type CertRow, type Inspection, type RuleChangeResult, type RuleTemplates } from '../api'
import { LEVEL_NAME, PASS_TITLE, rule } from '../plain'
import { Icon, ScopeBlock, useApp } from '../ui'

type Seed = Awaited<ReturnType<typeof api.seed>>
type Verify = Awaited<ReturnType<typeof api.verifyAll>>
type Tamper = Awaited<ReturnType<typeof api.tamper>>

export default function Regulator() {
  const { status, refreshStatus, catalog } = useApp()
  const [seed, setSeed] = useState<Seed | null>(null)
  const [verify, setVerify] = useState<Verify | null>(null)
  const [tamper, setTamper] = useState<Tamper | null>(null)
  const [certs, setCerts] = useState<CertRow[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [inspectId, setInspectId] = useState<string | null>(null)

  const loadCerts = useCallback(async () => setCerts(await api.certificates(false, 40)), [])
  useEffect(() => { loadCerts() }, [loadCerts, seed, verify])

  async function run<T>(name: string, fn: () => Promise<T>, set: (v: T) => void) {
    setBusy(name); setErr(null)
    try { set(await fn()); refreshStatus() } catch (e) { setErr(String(e).replace('Error: ', '')) } finally { setBusy(null) }
  }
  const total = status?.certificates ?? 0
  const pname = (id: string) => catalog[id]?.name ?? id

  return (
    <div>
      <div className="title-block">
        <h1 style={{ fontSize: 34 }}>Regulator view</h1>
        <p>The regulator runs its own node on the bank’s FISCO BCOS chain. It re-checks recommendations with its own verifier, so it never has to trust the bank’s software.</p>
      </div>
      {err && <div className="notice" style={{ marginBottom: 16 }}>{err}</div>}

      <div className="cols">
        <div className="card action-card">
          <span className="tag" style={{ alignSelf: 'flex-start' }}>Demo data</span>
          <h2>Simulate a day of advice</h2>
          <p className="muted">There are only a handful of real sessions on this laptop, so this generates ~10,000 realistic, made-up recommendations, so you can see how the checks scale. They’re marked as simulated everywhere.</p>
          <button className="btn" style={{ alignSelf: 'flex-start' }} disabled={!!busy} onClick={() => run('seed', () => api.seed(10_000), setSeed)}>
            {busy === 'seed' && <span className="spinner" />} Generate 10,000 simulated
          </button>
          {seed && <div className="result rise stack" style={{ gap: 4 }}>
            <span className="big">1 transaction</span>
            <span className="muted">{seed.count.toLocaleString()} certificates recorded on-chain in {seed.anchor_seconds}s (block {seed.block})</span>
          </div>}
        </div>

        <div className="card action-card">
          <h2>Check everything</h2>
          <p className="muted">For every recommendation on record, verify the bank’s signature and the blockchain record, and re-apply every rule from scratch.</p>
          <button className="btn primary" style={{ alignSelf: 'flex-start' }} disabled={!!busy || total === 0} onClick={() => run('verify', api.verifyAll, setVerify)}>
            {busy === 'verify' && <span className="spinner" />} Verify all {total ? total.toLocaleString() : ''}
          </button>
          {verify && <div className="result rise stack" style={{ gap: 4 }}>
            <span className="big">{verify.seconds}s</span>
            <span className="muted">{verify.ok.toLocaleString()} of {verify.total.toLocaleString()} verified · {verify.signature_failures + verify.recheck_failures + verify.unanchored} problems found</span>
          </div>}
        </div>
      </div>

      <RuleBuilder total={total} />

      <div className="cols" style={{ marginTop: 24 }}>
        <div className="card stack" style={{ gap: 12 }}>
          <div>
            <h2>Real client recommendations</h2>
            <p className="muted small" style={{ marginTop: 4 }}>From sessions on the Advisor tab. Inspect one to see every rule re-checked, or flag it to stop the sale.</p>
          </div>
          {certs.length === 0 && <div className="empty">No real recommendations yet. Give some advice on the Advisor tab first.</div>}
          {certs.slice().reverse().map(c => (
            <CertLine key={c.cert_id} c={c} pname={pname} selected={inspectId === c.cert_id}
                      onInspect={() => setInspectId(c.cert_id)} onChanged={loadCerts} />
          ))}
        </div>
        <div className="stack" style={{ gap: 24 }}>
          {inspectId ? <InspectPanel certId={inspectId} pname={pname} /> : (
            <div className="card"><h2>Inspect a certificate</h2>
              <p className="muted" style={{ marginTop: 6 }}>Pick a recommendation on the left. You’ll see exactly what the regulator’s verifier checks: the bank’s signature, the blockchain record, and every rule, one by one.</p></div>
          )}
          <div className="card action-card">
            <h2>Catch a cover-up</h2>
            <p className="muted">Quietly change the amount in a stored record, as someone hiding mis-selling might, then check it again.</p>
            <button className="btn" style={{ alignSelf: 'flex-start' }} disabled={!!busy || total === 0} onClick={() => run('tamper', () => api.tamper(inspectId ?? undefined), setTamper)}>
              Alter {inspectId ? 'the inspected' : 'the latest'} record
            </button>
            {tamper && <div className="result rise stack" style={{ gap: 4 }}>
              <span className="big" style={{ color: 'var(--bad)' }}>Caught</span>
              <span className="muted">{tamper.client_name}: {Object.entries(tamper.changed).map(([k, [a, b]]) => `${pname(k)} ${fmt.rmb(a)} → ${fmt.rmb(b)}`)}. The bank’s signature no longer matches{tamper.anchored_on_chain ? '' : ', and neither does the blockchain record'}.</span>
            </div>}
          </div>
        </div>
      </div>
    </div>
  )
}

function CertLine({ c, pname, selected, onInspect, onChanged }: {
  c: CertRow; pname: (id: string) => string; selected: boolean; onInspect: () => void; onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const status = c.stage === 'proposal' ? { t: 'Shown to client, not confirmed', cls: '' }
    : c.withdrawn ? { t: 'Withdrawn by the bank', cls: 'bad' }
    : c.flagged ? { t: c.executed ? 'Flagged after the order was placed' : 'Flagged: order system will refuse it', cls: 'bad' }
    : c.executed ? { t: 'Order placed', cls: 'good' }
    : { t: 'Confirmed, order not yet placed', cls: 'brand' }
  async function flag() {
    setBusy(true)
    try { await api.flag(c.cert_id, 'regulator review'); onChanged() } finally { setBusy(false) }
  }
  return (
    <div className={`case`} style={{ gap: 12, alignItems: 'center', background: selected ? 'var(--brand-soft)' : undefined, borderRadius: selected ? 12 : 0, padding: '12px 10px' }}>
      <div className="stack grow" style={{ gap: 3 }}>
        <b>{c.client_name}</b>
        <span className="small muted">{Object.entries(c.purchases).map(([k, v]) => `${pname(k)} ${fmt.rmb(v)}`).join(' · ')}</span>
        <span><span className={`tag ${status.cls}`}>{status.t}</span></span>
      </div>
      <div className="row" style={{ gap: 6 }}>
        <button className="btn sm" onClick={onInspect}>Inspect</button>
        {c.stage === 'execution' && !c.flagged && !c.withdrawn && <button className="btn sm" disabled={busy} onClick={flag}>{busy && <span className="spinner" />}Flag</button>}
      </div>
    </div>
  )
}

function InspectPanel({ certId, pname }: { certId: string; pname: (id: string) => string }) {
  const [r, setR] = useState<Inspection | null>(null)
  const [raw, setRaw] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setR(null); api.inspect(certId).then(setR).catch(e => setErr(String(e))) }, [certId])
  if (err) return <div className="card"><p style={{ color: 'var(--bad)' }}>{err}</p></div>
  if (!r) return <div className="card"><span className="spinner" /></div>
  const failed = r.constraints.filter(c => !c.passed)
  const groups = new Map<string, typeof r.constraints>()
  for (const c of r.constraints) groups.set(c.rule_id, [...(groups.get(c.rule_id) ?? []), c])
  const checks = [
    { ok: r.signature_ok, t: 'Signed by the bank’s key, unaltered' },
    { ok: r.rules_match, t: 'Issued under the same rule code the regulator runs' },
    { ok: r.anchored || r.stage === 'proposal', t: r.stage === 'proposal' ? 'Not yet confirmed, so not on-chain yet' : `Matches its blockchain record (block ${r.receipt?.block_number})` },
    { ok: r.recheck_ok, t: `Every rule re-applied: ${r.constraints.length - failed.length} of ${r.constraints.length} checks pass` },
  ]
  return (
    <div className="card stack rise" style={{ gap: 14 }}>
      <div className="spread">
        <div><h2>{r.client_name}</h2><span className="small muted">{r.stage === 'execution' ? 'Confirmed recommendation' : 'Recommendation shown to client'} · {r.issued_at.slice(0, 16).replace('T', ' ')}</span></div>
        <span className={`tag ${checks.every(c => c.ok) ? 'good' : 'bad'}`}>{checks.every(c => c.ok) ? 'Verified' : 'Problem found'}</span>
      </div>
      <div className="stack" style={{ gap: 6 }}>
        {checks.map(c => (
          <div key={c.t} className="row" style={{ gap: 10 }}>
            <span className={c.ok ? 'ok-ic' : 'no-ic'}>{c.ok ? Icon.check : Icon.x}</span><span className="small">{c.t}</span>
          </div>
        ))}
        {r.flagged && <div className="row" style={{ gap: 10 }}><span className="no-ic">!</span><span className="small">Flagged on-chain by the regulator</span></div>}
      </div>
      <div className="divider" />
      <div className="small muted">
        Facts on record: age {r.facts.age} · risk level {r.facts.assessed_level ? `C${r.facts.assessed_level} ${LEVEL_NAME[r.facts.assessed_level]}` : '-'}
        {r.facts.level_cap ? ` (capped at C${r.facts.level_cap})` : ''} · {fmt.rmb(r.facts.investable_assets)} investable · {r.facts.horizon_years}-year horizon. Identity is pseudonymised ({r.client_ref.slice(0, 10)}…).
      </div>
      <div>
        {[...groups.entries()].map(([id, items]) => {
          const pass = items.every(i => i.passed)
          return (
            <div key={id} className="check-row">
              <span className={pass ? 'ok-ic' : 'no-ic'}>{pass ? Icon.check : Icon.x}</span>
              <div className="stack" style={{ gap: 2 }}>
                <b style={{ fontSize: 14 }}>{pass ? (PASS_TITLE[id] ?? rule(id).title) : rule(id).title}</b>
                <span className="tiny faint">{items[0].citation}{items[0].kind === 'house' ? ' · bank policy, not law' : ''}</span>
                {items.some(i => i.product_id) && (
                  <span className="tiny muted">{items.filter(i => i.product_id && r.purchases[i.product_id]).map(i => pname(i.product_id!)).join(', ') || `checked for all ${items.length} products, none bought`}</span>
                )}
              </div>
              <span className="tiny faint">{items.length > 1 ? `${items.length} checks` : ''}</span>
            </div>
          )
        })}
      </div>
      <details className="why"><summary>What this certificate covers, and what it doesn’t</summary><ScopeBlock scope={r.scope} /></details>
      <button className="btn ghost sm" style={{ alignSelf: 'flex-start' }} onClick={() => setRaw(x => !x)}>{raw ? 'Hide' : 'Show'} the raw record</button>
      {raw && <InspectRaw certId={certId} />}
    </div>
  )
}

function InspectRaw({ certId }: { certId: string }) {
  const [doc, setDoc] = useState<string>('')
  useEffect(() => { api.certificate(certId).then(c => setDoc(JSON.stringify(c.cert, null, 2))) }, [certId])
  return <div className="raw">{doc || '…'}</div>
}

function RuleBuilder({ total }: { total: number }) {
  const { refreshStatus, catalog } = useApp()
  const [tpls, setTpls] = useState<RuleTemplates | null>(null)
  const [tpl, setTpl] = useState('ban_risk_for_age')
  const [params, setParams] = useState<Record<string, number>>({})
  const [res, setRes] = useState<RuleChangeResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => { api.ruleTemplates().then(setTpls) }, [])
  useEffect(() => {
    if (tpls?.[tpl]) setParams(Object.fromEntries(Object.entries(tpls[tpl].params).map(([k, v]) => [k, v.default])))
  }, [tpl, tpls])

  async function run() {
    setBusy(true); setErr(null)
    try { setRes(await api.ruleChange(tpl, params)); refreshStatus() } catch (e) { setErr(String(e).replace('Error: ', '')) } finally { setBusy(false) }
  }
  if (!tpls) return null
  const spec = tpls[tpl]

  return (
    <div className="card stack" style={{ gap: 16, marginTop: 24 }}>
      <div>
        <h2>A rule changes overnight</h2>
        <p className="muted small" style={{ marginTop: 4 }}>Write a new rule, then re-check every past recommendation against it. Today a compliance team does this by hand over weeks. These are simulated rules, not real law.</p>
      </div>
      <div className="choices" style={{ marginTop: 0 }}>
        {Object.entries(tpls).map(([k, v]) => <button key={k} className="choice" aria-pressed={tpl === k} onClick={() => { setTpl(k); setRes(null) }}>{v.label}</button>)}
      </div>
      <div className="grid-2">
        {Object.entries(spec.params).map(([k, p]) => (
          <div className="field" key={k}>
            <label>{p.label}{k === 'min_risk' ? ` (R${params[k] ?? p.default})` : ''}</label>
            {k === 'min_risk'
              ? <div className="choices" style={{ marginTop: 0 }}>{Array.from({ length: p.max - p.min + 1 }, (_, i) => p.min + i).map(n =>
                  <button key={n} className="choice" aria-pressed={params[k] === n} onClick={() => setParams(x => ({ ...x, [k]: n }))}>R{n}</button>)}</div>
              : <input className="input" inputMode="numeric" value={params[k] ?? ''} onChange={e => setParams(x => ({ ...x, [k]: Number(e.target.value.replace(/[^0-9]/g, '')) }))} />}
          </div>
        ))}
      </div>
      {err && <span className="small" style={{ color: 'var(--bad)' }}>{err}</span>}
      <button className="btn primary" style={{ alignSelf: 'flex-start' }} disabled={busy || total === 0} onClick={run}>
        {busy && <span className="spinner" />} Re-check all {total ? total.toLocaleString() : ''} recommendations
      </button>
      {res && (
        <div className="rise stack" style={{ gap: 10 }}>
          <div className="row wrap" style={{ gap: 28, alignItems: 'flex-end' }}>
            <div className="stat"><b>{res.affected_count.toLocaleString()}</b><span>recommendations break the new rule</span></div>
            <div className="stat"><b>{res.affected_real_clients}</b><span>of them real clients</span></div>
            <div className="stat"><b>{res.seconds}s</b><span>to check {res.checked.toLocaleString()}</span></div>
          </div>
          <p className="small">“{res.rule}”</p>
          {res.affected.length > 0 && (
            <div className="stack" style={{ gap: 2 }}>
              {res.affected.slice(0, 8).map(a => (
                <div key={a.cert_id} className="spread small" style={{ padding: '6px 0', borderTop: '1px solid var(--line)' }}>
                  <span>{a.real_client ? <b>{a.client_name}</b> : <span className="muted">{a.client_name}</span>}</span>
                  <span className="muted">{a.products.map(p => catalog[p]?.name ?? p).join(', ')}</span>
                </div>
              ))}
              {res.affected_count > 8 && <span className="tiny faint">and {(res.affected_count - 8).toLocaleString()} more</span>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
