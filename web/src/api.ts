export type Violation = {
  rule_id: string
  title: string
  citation: string
  kind: 'law' | 'house' | 'product'
  message: string
  product_id: string | null
}

export type SpeechIssue = {
  rule_id: string
  citation: string
  message: string
  excerpt: string
  product_id: string | null
}

export type Attempt = {
  source: 'llm' | 'llm-retry' | 'solver-fix'
  ok: boolean
  purchases: Record<string, number>
  message: string
  error: string | null
  violations: Violation[]
  speech: SpeechIssue[]
  sent_back?: string | null  // passed the rules, but wasn't useful advice, so the AI was asked again
  understood?: Understood | null  // the AI's own reading of the request, which the checks hold it to
}

export type Understood = {
  kind: 'new' | 'change' | 'alternative' | 'question' | 'other'
  amount: number | null
  products: string[]
  only: boolean
  exclude?: string[]
}

export type Scope = { verified: string; pattern_checked: string; not_checked: string[] }

export type AdviceResult = {
  attempts: Attempt[]
  final: Attempt
  first_pass_ok: boolean
  seconds: number
  model: string
  blocked_requests: Record<string, number>
  fix: null | { purchases: Record<string, number>; removed: Record<string, number>; added: Record<string, number>; required_procedure: string[] }
  certificate: null | {
    cert_id: string
    digest: string
    required_procedure: string[]
    rules: string[]
    checked: number
    scope: Scope
  }
  challenge?: Challenge
  integrity?: Integrity
  reassessment_required?: boolean
  rationale?: { points: string[]; figures: Record<string, unknown> }
  compare_default?: string[]
}

export type CompareRow = {
  id: string; name: string; type: string; market: string; risk: number; issuer_risk: number; distributor_risk: number
  annual_fee_pct: number; liquidity_days: number; liquidity: string; min_investment: number
  returns_pct: Record<string, number>; best_year_pct: number | null; worst_year_pct: number | null
  max_drawdown_pct: number | null; deposit_insured: boolean; private: boolean; complex: boolean
  suitable: boolean; max_amount: number; reasons: { rule_id: string; message: string; citation: string }[]
}

export type Integrity = {
  signals: { kind: string; excerpt: string; max_level: number | null; reported_change: boolean; source: string }[]
  assessed_level: number | null
  effective_level: number | null
  capped: boolean
  reported_change: boolean
}

export type Challenge = { attempts: number; blocked_drafts: number; breaks: number }

export type Product = {
  id: string
  name: string
  type: string
  market: 'CN' | 'HK'
  issuer_risk: number
  distributor_risk: number
  effective_risk: number
  min_investment: number
  liquidity_days: number
  annual_fee_bps: number
  returns_pct: Record<string, number>
  max_drawdown_pct: number | null
  private: boolean
  complex: boolean
  wmc_eligible: boolean
  description: string
}

export type ClientView = {
  id: string
  name: string
  blurb: string
  example: boolean
  markets: string[]
  level: number | null
  level_cap: number | null
  reported_change: boolean
  assessed_at: string | null
  assessment_valid: boolean
  facts: Record<string, unknown> & {
    age: number
    investable_assets: number
    monthly_expenses: number
    horizon_years: number
  }
  account: null | { cash: number; positions: Record<string, number> }
  orders: Order[]
  profile_flags: string[]
  unread: number
}

export type DirectoryEntry = { id: string; name: string; blurb: string; example: boolean; markets: string[]; level: number | null; investable_assets: number }

export type Order = { order_id: string; product_id: string; amount: number; status: string; settles_at: string | null; cancellable: boolean }

export type Note = { id: string; client_id: string; at: string; kind: string; title: string; text: string; action: string | null; read: boolean }

export type Case = {
  id: string; type: string; client_id: string; name: string; at: string; status: 'open' | 'resolved'
  title: string; summary: string; details: string[]; refs: Record<string, string>; actions: Record<string, string>
  resolution: null | { action: string; label: string; reason: string; effect: string; at: string }
}

export type CertRow = {
  cert_id: string; client_id: string; client_name: string; stage: string; purchases: Record<string, number>
  issued_at: string; tx_hash: string | null; synthetic: boolean; executed: boolean; withdrawn: boolean
  flagged: boolean; anchored: boolean
}

export type Inspection = {
  cert_id: string; client_name: string; stage: string; issued_at: string; rule_packs: { id: string; version: string }[]
  purchases: Record<string, number>
  facts: { age: number; assessed_level: number | null; level_cap: number | null; investable_assets: number; monthly_expenses: number; horizon_years: number }
  signature_ok: boolean; rules_match: boolean; recheck_ok: boolean; anchored: boolean; receipt: Receipt | null
  executed: boolean; withdrawn: boolean; flagged: boolean
  constraints: { rule_id: string; title: string; citation: string; kind: string; message: string; product_id: string | null; passed: boolean }[]
  client_ref: string; digest: string; scope: Scope
}

export type RuleTemplates = Record<string, { label: string; params: Record<string, { label: string; min: number; max: number; default: number }> }>

export type RuleChangeResult = {
  rule: string; citation: string; checked: number; affected_count: number; affected_real_clients: number
  affected: { cert_id: string; client_id: string; client_name: string; real_client: boolean; products: string[] }[]
  seconds: number
}

export type Receipt = {
  digest: string
  root: string
  proof: string[]
  batch_index: number
  tx_hash: string
  block_number: number
  chain_id: number
  contract: string
}

export type Status = {
  chain: { kind: string; url: string | null; chain_id: number; contract: string; error?: string; note?: string }
  model: string
  sentinel: string | null
  now: string
  certificates: number
  ledger_events: number
  ledger_ok: boolean
  compliance_queue: number
  challenge: Challenge
}

export type LedgerEvent = { seq: number; ts: string; type: string; data: Record<string, unknown>; prev: string; hash: string }

export type Question = { id: string; text: string; category: string; options: string[] }

export type Role = 'client' | 'bank' | 'regulator'
const TOKEN_KEY = 'pca-session'

type Session = { token: string; role: Role; client_id?: string | null }

export function getSession(): Session | null {
  try { const v = sessionStorage.getItem(TOKEN_KEY); return v ? JSON.parse(v) : null } catch { return null }
}

export function setSession(s: Session | null) {
  try { if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s)); else sessionStorage.removeItem(TOKEN_KEY) } catch { /* storage unavailable */ }
}

function authHeaders(): Record<string, string> {
  const s = getSession()
  return s ? { Authorization: `Bearer ${s.token}` } : {}
}

/** Demo sign-in. A client session is bound to one client; bank staff and regulators aren't. */
export async function signIn(role: Role, clientId?: string | null): Promise<Session> {
  const res = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role, client_id: role === 'client' ? clientId ?? null : null }) })
  if (!res.ok) throw new Error('sign-in failed')
  const s = await res.json()
  setSession(s)
  return s
}

async function call<T>(path: string, body?: unknown, retried = false): Promise<T> {
  const res = await fetch(path, body === undefined ? { headers: authHeaders() } : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  })
  const session = getSession()
  if (res.status === 401 && session && !retried) {  // expired token: sign in again as the same role, once
    await signIn(session.role, session.client_id)
    return call<T>(path, body, true)
  }
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not JSON */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export type ChatEntry = {
  request: string
  result: AdviceResult
  at: string
  flow?: { executionCert?: string; receipt?: Receipt; executableAfter?: string; exec?: { ok: boolean; reasons: string[]; escalated: boolean; orders: { order_id: string; product_id: string; amount: number; status: string; settles_at: string | null }[] } }
}

export const api = {
  status: () => call<Status>('/api/status'),
  catalog: () => call<Product[]>('/api/catalog'),
  clients: () => call<ClientView[]>('/api/clients'),
  directory: () => call<DirectoryEntry[]>('/api/directory'),
  client: (id: string) => call<ClientView>(`/api/clients/${id}`),
  questionnaire: () => call<Question[]>('/api/questionnaire'),
  compare: (clientId: string, productIds: string[]) => call<CompareRow[]>(`/api/clients/${clientId}/compare`, { product_ids: productIds }),
  createClient: (body: { name: string; age: number; investable_assets: number; monthly_expenses: number;
                         horizon_years: number; answers: Record<string, number>; annual_income?: number;
                         gba_resident?: boolean; investment_experience_years?: number;
                         net_household_financial_assets?: number }) =>
    call<ClientView & { score: number; knockout: boolean }>('/api/clients', body),
  assess: (id: string, answers: Record<string, number>) =>
    call<{ level: number; score: number; knockout: boolean; changed_from: number | null }>(
      `/api/clients/${id}/assessment`, { answers }),
  confirm: (id: string, body: { cert_id: string; acknowledged: string[]; level_change_confirmed?: boolean; enhanced_warning_ack?: boolean }) =>
    call<{ ok: boolean; cert_id?: string; digest?: string; receipt?: Receipt; executable_after?: string; violations?: Violation[] }>(
      `/api/clients/${id}/confirm`, body),
  execute: (cert_id: string) =>
    call<{ ok: boolean; reasons: string[]; escalated: boolean; orders: { order_id: string; product_id: string; amount: number; status: string; settles_at: string | null }[]; account: ClientView['account'] }>(
      '/api/execute', { cert_id }),
  resetDemo: () => call<{ ok: boolean; clients: number }>('/api/admin/reset', {}),
  advance: (hours: number) => call<{ now: string }>('/api/clock/advance', { hours }),
  ledger: () => call<{ ok: boolean; head: string; events: LedgerEvent[] }>('/api/ledger'),
  queue: () => call<Case[]>('/api/cases'),
  cases: (status: 'open' | 'resolved' | 'all') => call<Case[]>(`/api/cases?status=${status}`),
  resolveCase: (id: string, action: string, reason: string) => call<Case>(`/api/cases/${id}/resolve`, { action, reason }),
  redeem: (clientId: string, product_id: string, amount: number) =>
    call<{ order: Order; account: ClientView['account'] }>(`/api/clients/${clientId}/redeem`, { product_id, amount }),
  cancelOrder: (clientId: string, orderId: string) =>
    call<{ order: Order; account: ClientView['account'] }>(`/api/clients/${clientId}/orders/${encodeURIComponent(orderId)}/cancel`, {}),
  chat: (clientId: string) => call<ChatEntry[]>(`/api/clients/${clientId}/chat`),
  notifications: (clientId: string) => call<Note[]>(`/api/clients/${clientId}/notifications`),
  markRead: (clientId: string) => call<{ ok: boolean }>(`/api/clients/${clientId}/notifications/read`, {}),
  certificates: (includeSynthetic = false, limit = 50) => call<CertRow[]>(`/api/certificates?include_synthetic=${includeSynthetic}&limit=${limit}`),
  inspect: (certId: string) => call<Inspection>(`/api/regulator/inspect/${certId}`),
  ruleTemplates: () => call<RuleTemplates>('/api/regulator/rule-templates'),
  certificate: (id: string) => call<{ cert: Record<string, unknown>; stage: string; receipt: Receipt | null }>(`/api/certificates/${id}`),
  seed: (n: number) => call<{ count: number; issue_seconds: number; anchor_seconds: number; tx_hash: string; block: number }>('/api/regulator/seed', { n }),
  verifyAll: () => call<{ total: number; ok: number; signature_failures: number; recheck_failures: number; unanchored: number; seconds: number }>('/api/regulator/verify', {}),
  tamper: (cert_id?: string) => call<{ cert_id: string; client_name: string; changed: Record<string, [number, number]>; signature_ok: boolean; anchored_on_chain: boolean; problems: string[] }>('/api/regulator/tamper', { cert_id: cert_id ?? null }),
  flag: (cert_id: string, reason: string) => call<{ tx_hash: string; flagged: boolean }>('/api/regulator/flag', { cert_id, reason }),
  ruleChange: (template: string, params: Record<string, number>) => call<RuleChangeResult>('/api/regulator/rule-change', { template, params }),
  rerate: (product_id: string, distributor_risk: number) => call<{ product_id: string; from: number; to: number; affected: { client_id: string; name: string; level: number; holds: boolean; pending_certs: string[]; action: string }[] }>('/api/monitoring/rerate', { product_id, distributor_risk }),
}

/** POST + server-sent events. Calls onAttempt for each judged draft as it arrives. */
export async function advise(clientId: string, request: string, challenge: boolean,
                             onAttempt: (a: Attempt) => void): Promise<AdviceResult> {
  const res = await fetch(`/api/clients/${clientId}/advise`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ request, challenge }),
  })
  if (!res.ok || !res.body) throw new Error(`advise failed: ${res.status}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const event = /^event: (.*)$/m.exec(chunk)?.[1]
      const data = /^data: (.*)$/m.exec(chunk)?.[1]
      if (!event || data === undefined) continue
      const parsed = JSON.parse(data)
      if (event === 'attempt') onAttempt(parsed)
      if (event === 'result') return parsed as AdviceResult
      if (event === 'error') throw new Error(parsed.error)
    }
  }
  throw new Error('stream ended without a result')
}

export const DISCLOSURES: { id: string; label: string; article: string }[] = [
  { id: 'basic_info', label: 'Product basics: type, manager, investment scope, fees structure', article: 'Art. 31(1)' },
  { id: 'risk_level', label: 'Risk level, and that it may be adjusted during the product’s life', article: 'Art. 31(2)' },
  { id: 'principal_loss', label: 'Main risks, especially that principal can be lost', article: 'Art. 31(3)' },
  { id: 'fees', label: 'All fees and rates payable', article: 'Art. 31(4)' },
  { id: 'matching_opinion', label: 'The suitability matching opinion for you', article: 'Art. 31(5)' },
]

export const fmt = {
  rmb: (n: number) => `¥${Math.round(n).toLocaleString('en-US')}`,
  short: (h: string, n = 10) => (h.length > 2 * n ? `${h.slice(0, n)}…${h.slice(-6)}` : h),
}
