import { useEffect, useState } from 'react'
import { api, fmt, type Question } from '../api'
import { LEVEL_EXPLAIN, LEVEL_NAME } from '../plain'
import { Icon, navigate } from '../ui'

type Created = Awaited<ReturnType<typeof api.createClient>>

export default function NewClient() {
  const [qs, setQs] = useState<Question[]>([])
  const [step, setStep] = useState<1 | 2 | 3>(1)
  const [f, setF] = useState({ name: '', age: '35', investable_assets: '300000', monthly_expenses: '8000',
                               horizon_years: '3', annual_income: '200000', gba: false, exp: '3', nhfa: '1500000' })
  const [answers, setAnswers] = useState<Record<string, number>>({})
  const [created, setCreated] = useState<Created | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.questionnaire().then(setQs) }, [])
  const set = (k: keyof typeof f, v: string | boolean) => setF(x => ({ ...x, [k]: v }))
  const n = (v: string) => Math.max(0, Math.round(Number(v.replace(/[^0-9.]/g, '')) || 0))
  const basicsOk = f.name.trim() && n(f.age) >= 18 && n(f.age) <= 110 && n(f.investable_assets) > 0

  async function submit() {
    setBusy(true)
    setErr(null)
    try {
      setCreated(await api.createClient({
        name: f.name.trim(), age: n(f.age), investable_assets: n(f.investable_assets),
        monthly_expenses: n(f.monthly_expenses), horizon_years: n(f.horizon_years), answers,
        annual_income: n(f.annual_income), gba_resident: f.gba,
        investment_experience_years: f.gba ? n(f.exp) : 0, net_household_financial_assets: f.gba ? n(f.nhfa) : 0,
      }))
      setStep(3)
    } catch (e) {
      setErr(String(e).replace('Error: ', ''))
    } finally { setBusy(false) }
  }

  return (
    <div className="wizard stack" style={{ gap: 24 }}>
      <button className="btn ghost" style={{ alignSelf: 'flex-start' }} onClick={() => step === 2 ? setStep(1) : navigate('/')}>
        {Icon.back} {step === 2 ? 'Back to details' : 'Home'}
      </button>
      <div>
        <span className="faint small">Step {step} of 3</span>
        <h1 style={{ fontSize: 34, marginTop: 6 }}>
          {step === 1 ? 'Who is the client?' : step === 2 ? 'Risk questionnaire' : 'Assessment complete'}
        </h1>
        <p className="muted" style={{ marginTop: 8, fontSize: 16 }}>
          {step === 1 && 'Use anyone you like: yourself, a parent, a made-up person. Nothing leaves this machine.'}
          {step === 2 && 'Scored by code, not by the AI, so the AI can never nudge the result (NFRA Art. 13(1)).'}
          {step === 3 && 'This level decides which products the advisor may recommend.'}
        </p>
      </div>

      {step === 1 && (
        <div className="card stack" style={{ gap: 18 }}>
          <div className="field"><label>Name</label>
            <input className="input" value={f.name} onChange={e => set('name', e.target.value)} placeholder="e.g. Lin Yu" autoFocus /></div>
          <div className="grid-2">
            <div className="field"><label>Age</label><input className="input" inputMode="numeric" value={f.age} onChange={e => set('age', e.target.value)} /></div>
            <div className="field"><label>Investment horizon (years)</label><input className="input" inputMode="numeric" value={f.horizon_years} onChange={e => set('horizon_years', e.target.value)} /></div>
            <div className="field"><label>Money available to invest (¥)</label><input className="input" inputMode="numeric" value={f.investable_assets} onChange={e => set('investable_assets', e.target.value)} /></div>
            <div className="field"><label>Monthly expenses (¥)</label><input className="input" inputMode="numeric" value={f.monthly_expenses} onChange={e => set('monthly_expenses', e.target.value)} /></div>
            <div className="field"><label>Annual income (¥)</label><input className="input" inputMode="numeric" value={f.annual_income} onChange={e => set('annual_income', e.target.value)} /></div>
          </div>
          <label className="toggle">
            <input type="checkbox" checked={f.gba} onChange={e => set('gba', e.target.checked)} />
            Lives in the Greater Bay Area and wants Hong Kong funds (Wealth Management Connect)
          </label>
          {f.gba && (
            <div className="grid-2">
              <div className="field"><label>Years of investment experience</label><input className="input" value={f.exp} onChange={e => set('exp', e.target.value)} /></div>
              <div className="field"><label>Net household financial assets (¥)</label><input className="input" value={f.nhfa} onChange={e => set('nhfa', e.target.value)} /></div>
            </div>
          )}
          <div className="spread">
            <span className="faint small">{fmt.rmb(n(f.investable_assets))} to invest</span>
            <button className="btn primary" disabled={!basicsOk} onClick={() => {
              const age = n(f.age)
              setAnswers(a => ({ ...a, age: age < 30 ? 0 : age < 50 ? 1 : age < 65 ? 2 : 3 }))
              setStep(2)
            }}>Continue</button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          {qs.map((q, qi) => (
            <div className="q" key={q.id}>
              <b>{qi + 1}. {q.text}</b>
              <div className="choices">
                {q.options.map((o, i) => (
                  <button key={o} className="choice" aria-pressed={answers[q.id] === i}
                          onClick={() => setAnswers(a => ({ ...a, [q.id]: i }))}>{o}</button>
                ))}
              </div>
            </div>
          ))}
          {err && <p style={{ color: 'var(--bad)', marginTop: 12 }}>{err}</p>}
          <div className="spread" style={{ marginTop: 18 }}>
            <span className="faint small">{Object.keys(answers).length} of {qs.length} answered</span>
            <button className="btn primary" disabled={busy || Object.keys(answers).length < qs.length} onClick={submit}>
              {busy && <span className="spinner" />} Score the assessment
            </button>
          </div>
        </div>
      )}

      {step === 3 && created && created.level && (
        <div className="card stack rise" style={{ gap: 18 }}>
          <div className="row" style={{ gap: 20, alignItems: 'flex-end' }}>
            <span className="result-level" style={{ color: 'var(--brand)' }}>C{created.level}</span>
            <div>
              <h2>{LEVEL_NAME[created.level]}</h2>
              <p className="muted">{created.name} · score {created.score} / 45</p>
            </div>
          </div>
          <p style={{ fontSize: 16 }}>{LEVEL_EXPLAIN[created.level]}</p>
          {created.knockout && <p className="small muted">Because they can’t accept any loss, they’re placed in the lowest category.</p>}
          {created.markets.includes('HK') && <p className="small muted">Hong Kong funds are covered by the Wealth Management Connect rules as well.</p>}
          <button className="btn primary" style={{ alignSelf: 'flex-start' }} onClick={() => navigate(`/client/${created.id}`)}>
            Start advisory session {Icon.send}
          </button>
        </div>
      )}
    </div>
  )
}
