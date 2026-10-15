"""HTTP API. Run with: uv run uvicorn pca.api:app --port 8000"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import auth
from .profile import QUESTIONS, AssessmentLimitError
from .service import Service

app = FastAPI(title="Signet")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("PCA_CORS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
_svc: Service | None = None
_lock = threading.Lock()  # the service isn't thread-safe; serialise mutations


def svc() -> Service:
    global _svc
    if _svc is None:
        if os.environ.get("PCA_LLM") == "scripted":
            from .demo import DemoLLM

            _svc = Service(llm=DemoLLM())
        else:
            _svc = Service()
    return _svc


def _session_secret() -> bytes:
    s = svc()
    return s._secret(s.data / "keys" / "session.key", lambda: os.urandom(32))


def need(*roles: str):
    """Dependency: the caller's token must grant at least one of ``roles``."""
    def check(authorization: str | None = Header(default=None)) -> set[str]:
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        granted = auth.roles_of(token, _session_secret())
        if not granted:
            raise HTTPException(401, "sign in first (choose a role)")
        if not granted & set(roles):
            raise HTTPException(403, f"this needs the {' or '.join(roles)} role")
        return granted
    return Depends(check)


def _claims(authorization: str | None) -> tuple[set[str], str | None]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    return auth.claims(token, _session_secret())


def own_client(client_id: str, authorization: str | None = Header(default=None)) -> None:
    """Dependency for /api/clients/{client_id}/...: bank staff, or the client this session belongs to."""
    roles, subject = _claims(authorization)
    if not roles:
        raise HTTPException(401, "sign in first (choose a role)")
    if "bank" in roles:
        return
    if "client" not in roles:
        raise HTTPException(403, "this needs the client or bank role")
    if subject != client_id:
        raise HTTPException(403, "this session belongs to a different client")


CLIENT = [need("client", "bank")]
OWN = [Depends(own_client)]
BANK = [need("bank")]
REGULATOR = [need("regulator")]
BANK_OR_REGULATOR = [need("bank", "regulator")]


class SessionIn(BaseModel):
    role: str
    client_id: str | None = None  # a client session is bound to one client


@app.post("/api/session")
def session(body: SessionIn):
    """Demo sign-in: pick a role, get a signed session token (no passwords; see auth.py)."""
    try:
        sub = body.client_id if body.role == "client" else None
        if sub is not None and sub not in svc().personas:
            raise HTTPException(404, "no such client")
        return {"token": auth.issue([body.role], _session_secret(), subject=sub), "role": body.role,
                "client_id": sub}
    except ValueError as e:
        raise HTTPException(400, str(e))


def _guard(fn, *a, **kw):
    try:
        with _lock:
            return fn(*a, **kw)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}")
    except (ValueError, AssessmentLimitError) as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- read

@app.get("/api/status")
def status():
    s = svc()
    return {"chain": s.chain, "model": s.llm.name, "sentinel": s.sentinel.model if s.sentinel else None, "now": s.now().isoformat(), "certificates": len(s.certs),
            "ledger_events": len(s.ledger.events), "ledger_ok": s.ledger.verify()[0],
            "compliance_queue": len(s.list_cases("open")), "challenge": s.challenge}


@app.get("/api/catalog")
def catalog():
    return [{**p.model_dump(mode="json"), "effective_risk": p.effective_risk} for p in svc().catalog.values()]


@app.get("/api/questionnaire")
def questionnaire():
    return [{"id": q.id, "text": q.text, "category": q.category, "options": [o[0] for o in q.options]}
            for q in QUESTIONS]


@app.get("/api/directory")
def directory():
    """Demo sign-in chooser: who you can sign in as. A summary card only; no account, orders or conversation."""
    s = svc()
    out = []
    for cid, p in s.personas.items():
        a = p.client.current_assessment
        out.append({"id": cid, "name": p.name, "blurb": p.blurb, "example": p.example,
                    "markets": [m.value for m in p.markets], "level": a.level if a else None,
                    "investable_assets": p.client.investable_assets})
    return out


@app.get("/api/clients")
def clients(authorization: str | None = Header(default=None)):
    roles, subject = _claims(authorization)
    if not roles:
        raise HTTPException(401, "sign in first (choose a role)")
    s = svc()
    if "bank" in roles:
        return [s.client_view(cid) for cid in s.personas]
    if "client" in roles:
        return [s.client_view(subject)] if subject in s.personas else []
    raise HTTPException(403, "this needs the client or bank role")


@app.get("/api/clients/{client_id}", dependencies=OWN)
def client(client_id: str):
    return _guard(svc().client_view, client_id)


@app.get("/api/ledger", dependencies=BANK_OR_REGULATOR)
def ledger(limit: int = 200):
    s = svc()
    return {"ok": s.ledger.verify()[0], "head": s.ledger.head,
            "events": [e.__dict__ for e in s.ledger.events[-limit:]]}


@app.get("/api/compliance/queue", dependencies=BANK)
def compliance_queue():
    return svc().list_cases("open")


@app.get("/api/cases", dependencies=BANK)
def cases(status: str = "open"):
    if status not in ("open", "resolved", "all"):
        raise HTTPException(400, "status must be open, resolved or all")
    return svc().list_cases(status)


class ResolveIn(BaseModel):
    action: str
    reason: str


@app.post("/api/cases/{case_id}/resolve", dependencies=BANK)
def resolve_case(case_id: str, body: ResolveIn):
    return _guard(svc().resolve_case, case_id, body.action, body.reason)


@app.get("/api/clients/{client_id}/chat", dependencies=OWN)
def chat(client_id: str):
    return _guard(svc().chat, client_id)


class RedeemIn(BaseModel):
    product_id: str
    amount: int


@app.post("/api/clients/{client_id}/redeem", dependencies=OWN)
def redeem(client_id: str, body: RedeemIn):
    return _guard(svc().redeem, client_id, body.product_id, body.amount)


@app.post("/api/clients/{client_id}/orders/{order_id}/cancel", dependencies=OWN)
def cancel_order(client_id: str, order_id: str):
    return _guard(svc().cancel_order, client_id, order_id)


@app.get("/api/clients/{client_id}/notifications", dependencies=OWN)
def notifications(client_id: str):
    return _guard(svc().notifications, client_id)


@app.post("/api/clients/{client_id}/notifications/read", dependencies=OWN)
def notifications_read(client_id: str):
    _guard(svc().mark_read, client_id)
    return {"ok": True}


@app.get("/api/certificates/{cert_id}", dependencies=BANK_OR_REGULATOR)
def certificate(cert_id: str):
    s = svc().certs.get(cert_id)
    if s is None:
        raise HTTPException(404, "certificate not found")
    return {"cert": s.cert, "stage": s.stage, "receipt": asdict(s.receipt) if s.receipt else None}


@app.get("/api/certificates", dependencies=BANK_OR_REGULATOR)
def certificates(include_synthetic: bool = False, limit: int = 50):
    return _guard(svc().list_certs, include_synthetic, min(limit, 500))


@app.get("/api/regulator/inspect/{cert_id}", dependencies=REGULATOR)
def inspect(cert_id: str):
    return _guard(svc().inspect, cert_id)


@app.get("/api/regulator/rule-templates")
def rule_templates():
    return svc().rule_templates()


# ---------------------------------------------------------------- client flow

class NewClientIn(BaseModel):
    name: str
    age: int
    investable_assets: int
    monthly_expenses: int
    horizon_years: int
    answers: dict[str, int]
    annual_income: int = 0
    gba_resident: bool = False
    investment_experience_years: int = 0
    net_household_financial_assets: int = 0


@app.post("/api/clients", dependencies=CLIENT)
def create_client(body: NewClientIn):
    from pydantic import ValidationError

    try:
        return _guard(svc().create_client, **body.model_dump())
    except ValidationError as e:
        raise HTTPException(400, str(e))


class AssessIn(BaseModel):
    answers: dict[str, int]


@app.post("/api/clients/{client_id}/assessment", dependencies=OWN)
def reassess(client_id: str, body: AssessIn):
    return _guard(svc().reassess, client_id, body.answers)


class AdviseIn(BaseModel):
    request: str
    challenge: bool = False


@app.post("/api/clients/{client_id}/advise", dependencies=OWN)
async def advise(client_id: str, body: AdviseIn):
    """Server-sent events: one ``attempt`` event per judged draft, then ``result``."""
    s = svc()
    if client_id not in s.personas:
        raise HTTPException(404, "client not found")
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def work():
        try:
            with _lock:
                out = s.advise(client_id, body.request,
                               on_attempt=lambda a: loop.call_soon_threadsafe(q.put_nowait, ("attempt", a)),
                               challenge=body.challenge)
            loop.call_soon_threadsafe(q.put_nowait, ("result", out))
        except Exception as e:  # surface model or runtime errors to the UI
            loop.call_soon_threadsafe(q.put_nowait, ("error", {"error": str(e)}))

    threading.Thread(target=work, daemon=True).start()

    async def stream():
        yield "event: started\ndata: {}\n\n"
        while True:
            kind, data = await q.get()
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            if kind in ("result", "error"):
                break

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class CompareIn(BaseModel):
    product_ids: list[str]


@app.post("/api/clients/{client_id}/compare", dependencies=OWN)
def compare_products(client_id: str, body: CompareIn):
    return _guard(svc().compare_products, client_id, body.product_ids)


class ConfirmIn(BaseModel):
    cert_id: str
    acknowledged: list[str]
    level_change_confirmed: bool = False
    enhanced_warning_ack: bool = False


@app.post("/api/clients/{client_id}/confirm", dependencies=OWN)
def confirm(client_id: str, body: ConfirmIn):
    return _guard(svc().confirm, client_id, body.cert_id, body.acknowledged, body.level_change_confirmed,
                  body.enhanced_warning_ack)


class ExecuteIn(BaseModel):
    cert_id: str


@app.post("/api/execute")
def execute(body: ExecuteIn, authorization: str | None = Header(default=None)):
    roles, subject = _claims(authorization)
    if not roles:
        raise HTTPException(401, "sign in first (choose a role)")
    if "bank" not in roles:
        if "client" not in roles:
            raise HTTPException(403, "this needs the client or bank role")
        stored = svc().certs.get(body.cert_id)
        if stored is None or stored.client_id != subject:
            raise HTTPException(403, "this certificate belongs to a different client")
    return _guard(svc().execute, body.cert_id)


class ClockIn(BaseModel):
    hours: float


@app.post("/api/clock/advance", dependencies=CLIENT)
def advance(body: ClockIn):
    return {"now": _guard(svc().advance_clock, body.hours).isoformat()}


# ---------------------------------------------------------------- regulator and monitoring

@app.post("/api/admin/reset", dependencies=BANK)
def reset_demo():
    return _guard(svc().reset_demo)


class SeedIn(BaseModel):
    n: int = 10_000


@app.post("/api/regulator/seed", dependencies=REGULATOR)
def seed(body: SeedIn):
    return _guard(svc().seed_synthetic, min(body.n, 50_000))


@app.post("/api/regulator/verify", dependencies=REGULATOR)
def verify_all():
    return _guard(svc().verify_all)


class TamperIn(BaseModel):
    cert_id: str | None = None


@app.post("/api/regulator/tamper", dependencies=REGULATOR)
def tamper(body: TamperIn):
    return _guard(svc().tamper_demo, body.cert_id)


class FlagIn(BaseModel):
    cert_id: str
    reason: str


@app.post("/api/regulator/flag", dependencies=REGULATOR)
def flag(body: FlagIn):
    return _guard(svc().flag, body.cert_id, body.reason)


class RuleChangeIn(BaseModel):
    template: str | None = None
    params: dict[str, int] | None = None
    change: str | None = None  # preset name


@app.post("/api/regulator/rule-change", dependencies=REGULATOR)
def rule_change(body: RuleChangeIn):
    return _guard(svc().rule_change, body.template, body.params, body.change)


class RerateIn(BaseModel):
    product_id: str
    distributor_risk: int


@app.post("/api/monitoring/rerate", dependencies=BANK)
def rerate(body: RerateIn):
    return _guard(svc().rerate, body.product_id, body.distributor_risk)


# ---------------------------------------------------------------- web UI (built by `npm run build` in web/)

_STATIC = Path(__file__).parent / "static"
if _STATIC.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="ui")
