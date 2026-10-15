import { useEffect, useState } from 'react'
import { api, fmt, type DirectoryEntry } from '../api'
import { LEVEL_NAME } from '../plain'
import { Avatar, Icon, navigate, Risk } from '../ui'

export default function Home() {
  const [clients, setClients] = useState<DirectoryEntry[]>([])
  useEffect(() => { api.directory().then(setClients).catch(() => {}) }, [])
  const examples = clients.filter(c => c.example)
  const mine = clients.filter(c => !c.example)

  return (
    <div className="stack" style={{ gap: 48 }}>
      <section className="hero">
        <div>
          <span className="tag brand">For banks deploying AI advisors</span>
          <h1 style={{ marginTop: 18 }}>AI wealth advice, checked against the rules before the client sees it.</h1>
          <p className="lead">
            The AI writes the advice. Before the client sees a single word, it’s checked against China’s
            investor-suitability rules. If it fails, the client never sees it. If it passes, the bank gets a
            certificate that a regulator can verify on its own.
          </p>
          <div className="row wrap" style={{ marginTop: 28, gap: 12 }}>
            <button className="btn primary" onClick={() => navigate('/new')}>{Icon.user} Start with a new client</button>
            {examples[0] && <button className="btn" onClick={() => navigate(`/client/${examples[0].id}`)}>Try an example</button>}
          </div>
        </div>
        <div className="steps">
          <div className="step">
            <span className="step-n">1</span>
            <b>The AI drafts advice</b>
            <p className="small muted">A local model talks to the client and proposes products and amounts.</p>
          </div>
          <div className="step">
            <span className="step-n">2</span>
            <b>Every draft is checked</b>
            <p className="small muted">Risk matching, guarantee promises, deposit confusion and more, under NFRA Order No. 7 (2025).</p>
          </div>
          <div className="step">
            <span className="step-n">3</span>
            <b>Only proven advice ships</b>
            <p className="small muted">The client gets advice that passed, and the proof is anchored on FISCO BCOS.</p>
          </div>
        </div>
      </section>

      {mine.length > 0 && (
        <section className="stack">
          <h2>Your clients</h2>
          <div className="examples">
            {mine.map(c => <Person key={c.id} c={c} />)}
          </div>
        </section>
      )}

      <section className="stack">
        <div className="spread wrap">
          <div>
            <h2>Example clients</h2>
            <p className="muted" style={{ marginTop: 4 }}>Each one shows a different way an AI advisor can go wrong.</p>
          </div>
          <button className="btn ghost" onClick={() => navigate('/new')}>+ Create your own</button>
        </div>
        <div className="examples">
          {examples.map(c => <Person key={c.id} c={c} />)}
        </div>
      </section>
    </div>
  )
}

function Person({ c }: { c: DirectoryEntry }) {
  return (
    <button className="person" onClick={() => navigate(`/client/${c.id}`)}>
      <Avatar name={c.name} />
      <span className="stack" style={{ gap: 4 }}>
        <span className="row" style={{ gap: 8 }}>
          <b style={{ fontSize: 16 }}>{c.name}</b>
          {c.level && <Risk level={c.level} prefix="C" />}
        </span>
        <span className="small muted">{c.blurb}</span>
        <span className="tiny faint">
          {c.level ? LEVEL_NAME[c.level] : 'Not assessed'} · {fmt.rmb(c.investable_assets)}
          {c.markets.includes('HK') ? ' · Hong Kong access' : ''}
        </span>
      </span>
    </button>
  )
}
