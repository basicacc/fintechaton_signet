"""Verifier: check proposals, explain violations, and search for legal alternatives.

- ``check``         plain evaluator, lists every violated constraint (used by the auditor)
- ``check_z3``      same question via Z3; returns an unsat core (minimal blocking set)
- ``minimal_fix``   nearest compliant purchase set (Z3 Optimize, lexicographic)
- ``max_amount``    the most the client may put into one product, all else fixed
- ``feasible``      can any non-empty purchase satisfy all active packs at once?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

import z3

from .expr import Expr
from .models import Client, Market, Procedure, Product, Proposal, DISCLOSURE_ITEMS
from .rules import Constraint, Context, Kind, Rule, RulePack, Stage, amt


@dataclass(frozen=True)
class Violation:
    rule_id: str
    title: str
    citation: str
    kind: Kind
    message: str
    product_id: str | None = None


@dataclass
class CheckResult:
    ok: bool
    stage: Stage
    violations: list[Violation]
    checked: int  # number of constraint instances evaluated
    rules: list[str]  # rule ids that were active


@dataclass
class Fix:
    purchases: dict[str, int]
    removed: dict[str, int] = field(default_factory=dict)
    added: dict[str, int] = field(default_factory=dict)
    required_procedure: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- building

def _scope_constraints(ctx: Context, packs: Sequence[RulePack]) -> list[tuple[Rule, Constraint]]:
    """Products whose market no active legal pack covers may not be bought."""
    covered = set().union(*(p.markets for p in packs if any(r.kind == Kind.LAW for r in p.rules)))
    rule = Rule("SCOPE", "Product outside active jurisdictions", "System scope", Kind.LAW, Stage.PROPOSAL, lambda c: [])
    out = []
    for p in ctx.products():
        if p.market not in covered:
            out.append((rule, Constraint("SCOPE", amt(p.id).eq(0),
                                         f"No active rule pack covers {p.market.value} products.", p.id)))
    return out


def build_constraints(ctx: Context, packs: Sequence[RulePack], stage: Stage) -> list[tuple[Rule, Constraint]]:
    stages = {Stage.PROPOSAL} if stage == Stage.PROPOSAL else {Stage.PROPOSAL, Stage.EXECUTION}
    seen: set[str] = set()
    out: list[tuple[Rule, Constraint]] = []
    for pack in packs:
        for rule in pack.rules:
            if rule.stage not in stages:
                continue
            for c in rule.build(ctx):
                if c.label in seen:  # same rule shared by packs (e.g. P-MIN)
                    continue
                seen.add(c.label)
                out.append((rule, c))
    out.extend(_scope_constraints(ctx, packs))
    return out


def _env(ctx: Context, purchases: dict[str, int], procedure: Procedure) -> dict[str, int | bool]:
    env: dict[str, int | bool] = {f"amt:{pid}": purchases.get(pid, 0) for pid in ctx.catalog}
    env["proc:enhanced_warning"] = procedure.enhanced_warning
    env["proc:consideration_hours"] = procedure.consideration_hours
    env["proc:follow_up_scheduled"] = procedure.follow_up_scheduled
    env["proc:client_confirmed"] = procedure.client_confirmed
    env["proc:level_change_confirmed"] = procedure.level_change_confirmed
    for d in DISCLOSURE_ITEMS:
        env[f"proc:disclosure:{d}"] = d in procedure.disclosures_shown
    return env


def _z3_vars(ctx: Context) -> dict[str, z3.ExprRef]:
    vs: dict[str, z3.ExprRef] = {f"amt:{pid}": z3.Int(f"amt:{pid}") for pid in ctx.catalog}
    for name in ("enhanced_warning", "follow_up_scheduled", "client_confirmed", "level_change_confirmed"):
        vs[f"proc:{name}"] = z3.Bool(f"proc:{name}")
    vs["proc:consideration_hours"] = z3.Int("proc:consideration_hours")
    for d in DISCLOSURE_ITEMS:
        vs[f"proc:disclosure:{d}"] = z3.Bool(f"proc:disclosure:{d}")
    return vs


def _validate(ctx: Context, purchases: dict[str, int]) -> None:
    unknown = set(purchases) - set(ctx.catalog)
    if unknown:
        raise ValueError(f"unknown products: {sorted(unknown)}")


# ---------------------------------------------------------------- checking

def check(client: Client, catalog: dict[str, Product], proposal: Proposal,
          packs: Sequence[RulePack], stage: Stage = Stage.PROPOSAL) -> CheckResult:
    ctx = Context(client, catalog, proposal.as_of)
    _validate(ctx, proposal.purchases)
    env = _env(ctx, proposal.purchases, proposal.procedure)
    pairs = build_constraints(ctx, packs, stage)
    violations = [
        Violation(r.id, r.title, r.citation, r.kind, c.message, c.product_id)
        for r, c in pairs
        if not c.expr.evaluate(env)
    ]
    rule_ids = sorted({r.id for r, _ in pairs})
    return CheckResult(not violations, stage, violations, len(pairs), rule_ids)


def check_z3(client: Client, catalog: dict[str, Product], proposal: Proposal,
             packs: Sequence[RulePack], stage: Stage = Stage.PROPOSAL) -> tuple[bool, list[str]]:
    """Returns (ok, unsat_core_labels)."""
    ctx = Context(client, catalog, proposal.as_of)
    _validate(ctx, proposal.purchases)
    env = _env(ctx, proposal.purchases, proposal.procedure)
    vs = _z3_vars(ctx)
    s = z3.Solver()
    for name, v in vs.items():  # pin every variable to the proposal's values
        val = env[name]
        s.add(v == (z3.BoolVal(val) if isinstance(val, bool) else z3.IntVal(val)))
    trackers = {}
    for _, c in build_constraints(ctx, packs, stage):
        t = z3.Bool(f"track:{c.label}")
        trackers[str(t)] = c.label
        s.assert_and_track(c.expr.to_z3(vs), t)
    if s.check() == z3.sat:
        return True, []
    return False, sorted(trackers[str(t)] for t in s.unsat_core())


# ---------------------------------------------------------------- search

def _base_solver(ctx: Context, packs: Sequence[RulePack], opt: bool):
    vs = _z3_vars(ctx)
    s = z3.Optimize() if opt else z3.Solver()
    for pid in ctx.catalog:
        s.add(vs[f"amt:{pid}"] >= 0)
    for _, c in build_constraints(ctx, packs, Stage.PROPOSAL):
        s.add(c.expr.to_z3(vs))
    return s, vs


def _model_purchases(m: z3.ModelRef, vs, ctx: Context) -> dict[str, int]:
    out = {}
    for pid in ctx.catalog:
        v = m.eval(vs[f"amt:{pid}"], model_completion=True).as_long()
        if v:
            out[pid] = v
    return out


LOT = 1_000  # solver plans use whole thousands of RMB


def minimal_fix(client: Client, catalog: dict[str, Product], proposal: Proposal,
                packs: Sequence[RulePack]) -> Fix | None:
    """Nearest compliant purchase set.

    Objectives, in priority order:
    1. keep the total invested as close as possible to what was proposed
    2. move as little money as possible (L1 distance, additions weighted to prefer
       products of the same type and closest risk to what was removed)
    """
    ctx = Context(client, catalog, proposal.as_of)
    _validate(ctx, proposal.purchases)
    s, vs = _base_solver(ctx, packs, opt=True)
    want = proposal.purchases
    target = sum(want.values())
    # Substitutes should resemble what got blocked, not the whole basket.
    blocked = {v.product_id for v in check(client, catalog, proposal, packs).violations if v.product_id}
    basis = [p for p in want if p in blocked] or list(want)
    wanted_types = {catalog[p].type for p in basis}
    wanted_risk = max((catalog[p].effective_risk for p in basis), default=1)

    for pid in ctx.catalog:  # whole thousands, like a real advisor: no ¥149,999 plans
        s.add(vs[f"amt:{pid}"] % LOT == 0)
    tot = z3.Sum([vs[f"amt:{p}"] for p in ctx.catalog])
    gap = z3.Int("gap")
    s.add(gap >= target - tot, gap >= tot - target)

    # Removing money costs 1 per RMB. Adding money costs more the less the product
    # resembles what the client asked for, so freed-up money flows to the closest
    # suitable substitute rather than defaulting to cash.
    dist_terms = []
    for pid, prod in ctx.catalog.items():
        a, x = vs[f"amt:{pid}"], want.get(pid, 0)
        up, down = z3.Int(f"up:{pid}"), z3.Int(f"down:{pid}")
        s.add(up >= 0, down >= 0, up >= a - x, down >= x - a)
        w_up = 1 + 4 * (prod.type not in wanted_types) + abs(wanted_risk - prod.effective_risk)
        dist_terms += [down, w_up * up]

    positions = z3.Sum([z3.If(vs[f"amt:{p}"] > 0, 1, 0) for p in ctx.catalog])

    s.minimize(gap)
    s.minimize(z3.Sum(dist_terms))
    s.minimize(positions)  # tie-break: no dust positions
    if s.check() != z3.sat:
        return None
    got = _model_purchases(s.model(), vs, ctx)
    removed = {p: want[p] - got.get(p, 0) for p in want if got.get(p, 0) < want[p]}
    added = {p: got[p] - want.get(p, 0) for p in got if got[p] > want.get(p, 0)}
    return Fix(got, removed, added, required_procedure(client, catalog, got, proposal.as_of, packs))


def max_amount(client: Client, catalog: dict[str, Product], proposal: Proposal,
               packs: Sequence[RulePack], product_id: str) -> int:
    """Largest amount of ``product_id`` allowed with the other purchases unchanged."""
    ctx = Context(client, catalog, proposal.as_of)
    _validate(ctx, {**proposal.purchases, product_id: 0})
    s, vs = _base_solver(ctx, packs, opt=True)
    for pid in ctx.catalog:
        if pid != product_id:
            s.add(vs[f"amt:{pid}"] == proposal.purchases.get(pid, 0))
    a = vs[f"amt:{product_id}"]
    s.add(a <= client.investable_assets)  # bound the search
    s.maximize(a)
    if s.check() != z3.sat:
        return 0
    return s.model().eval(a, model_completion=True).as_long()


def feasible(client: Client, catalog: dict[str, Product], as_of: datetime,
             packs: Sequence[RulePack], market: Market | None = None) -> tuple[bool, list[str]]:
    """Is there any non-empty purchase (optionally in one market) satisfying every pack?

    When infeasible, returns the unsat core: the minimal set of rules that together
    make every purchase impossible. This is how the system detects rule packs that
    conflict for a given client (e.g. mainland + Southbound together).
    """
    ctx = Context(client, catalog, as_of)
    vs = _z3_vars(ctx)
    s = z3.Solver()
    for pid in ctx.catalog:
        s.add(vs[f"amt:{pid}"] >= 0)
    trackers = {}
    for _, c in build_constraints(ctx, packs, Stage.PROPOSAL):
        t = z3.Bool(f"track:{c.label}")
        trackers[str(t)] = c.label
        s.assert_and_track(c.expr.to_z3(vs), t)
    s.add(ctx.total_purchases(market).to_z3(vs) > 0)
    if s.check() == z3.sat:
        return True, []
    return False, sorted(trackers[str(t)] for t in s.unsat_core())


def required_procedure(client: Client, catalog: dict[str, Product], purchases: dict[str, int],
                       as_of: datetime, packs: Sequence[RulePack]) -> list[str]:
    """Which execution-stage steps this purchase set triggers (shown to the client up front)."""
    ctx = Context(client, catalog, as_of)
    steps = []
    for _, c in build_constraints(ctx, packs, Stage.EXECUTION):
        free = c.expr.free_vars()
        if any(v.startswith("proc:") for v in free):
            # Does the constraint bind for this purchase set when no procedure was done?
            env = _env(ctx, purchases, Procedure())
            if not c.expr.evaluate(env):
                steps.append(c.rule_id)
    return sorted(set(steps))


def default_packs(markets: Iterable[Market]) -> list[RulePack]:
    from .rules import CN_NFRA, HK_WMC, HOUSE

    ms = set(markets)
    packs = []
    if Market.CN in ms:
        packs.append(CN_NFRA)
    if Market.HK in ms:
        packs.append(HK_WMC)
    packs.append(HOUSE)
    return packs


@dataclass(frozen=True)
class ConstraintResult:
    rule_id: str
    title: str
    citation: str
    kind: Kind
    message: str
    product_id: str | None
    passed: bool


def explain(client: Client, catalog: dict[str, Product], proposal: Proposal, packs: Sequence[RulePack],
            stage: Stage = Stage.PROPOSAL) -> list[ConstraintResult]:
    """Every constraint instance with its outcome, for audit views (plain evaluator only)."""
    ctx = Context(client, catalog, proposal.as_of)
    env = _env(ctx, proposal.purchases, proposal.procedure)
    return [ConstraintResult(r.id, r.title, r.citation, r.kind, c.message, c.product_id, bool(c.expr.evaluate(env)))
            for r, c in build_constraints(ctx, packs, stage)]
