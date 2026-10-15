"""Tiny rule expression language.

Rules are written once as expression trees and compiled two ways:
- ``to_z3``: for search (minimal legal fix, max allowable amount, joint feasibility)
- ``evaluate``: a plain-Python evaluator used by the independent verifier

Tests assert the two backends agree, so auditors never have to trust the solver.
All arithmetic is integer (amounts in whole RMB), so there are no float rounding issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import z3

Env = Mapping[str, int | bool]


class Expr:
    def to_z3(self, vars: Mapping[str, z3.ExprRef]) -> z3.ExprRef:
        raise NotImplementedError

    def evaluate(self, env: Env) -> int | bool:
        raise NotImplementedError

    def free_vars(self) -> set[str]:
        raise NotImplementedError

    # arithmetic
    def __add__(self, o): return Add((self, lift(o)))
    def __radd__(self, o): return Add((lift(o), self))
    def __sub__(self, o): return Add((self, Mul(-1, lift(o))))
    def __rsub__(self, o): return Add((lift(o), Mul(-1, self)))
    def __mul__(self, k: int): return Mul(k, self)
    def __rmul__(self, k: int): return Mul(k, self)

    # comparisons build Cmp nodes (not Python bools)
    def __le__(self, o): return Cmp("<=", self, lift(o))
    def __lt__(self, o): return Cmp("<", self, lift(o))
    def __ge__(self, o): return Cmp(">=", self, lift(o))
    def __gt__(self, o): return Cmp(">", self, lift(o))
    def eq(self, o): return Cmp("==", self, lift(o))


def lift(v) -> Expr:
    if isinstance(v, Expr):
        return v
    if isinstance(v, bool):
        return BoolConst(v)
    if isinstance(v, int):
        return Const(v)
    raise TypeError(f"cannot lift {v!r} into an expression")


@dataclass(frozen=True)
class Const(Expr):
    value: int

    def to_z3(self, vars): return z3.IntVal(self.value)
    def evaluate(self, env): return self.value
    def free_vars(self): return set()


@dataclass(frozen=True)
class BoolConst(Expr):
    value: bool

    def to_z3(self, vars): return z3.BoolVal(self.value)
    def evaluate(self, env): return self.value
    def free_vars(self): return set()


@dataclass(frozen=True)
class Var(Expr):
    name: str
    is_bool: bool = False

    def to_z3(self, vars): return vars[self.name]
    def evaluate(self, env): return env[self.name]
    def free_vars(self): return {self.name}


@dataclass(frozen=True)
class Add(Expr):
    terms: tuple[Expr, ...]

    def to_z3(self, vars): return z3.Sum([t.to_z3(vars) for t in self.terms]) if self.terms else z3.IntVal(0)
    def evaluate(self, env): return sum(t.evaluate(env) for t in self.terms)
    def free_vars(self): return set().union(*(t.free_vars() for t in self.terms)) if self.terms else set()


@dataclass(frozen=True)
class Mul(Expr):
    k: int
    e: Expr

    def to_z3(self, vars): return self.k * self.e.to_z3(vars)
    def evaluate(self, env): return self.k * self.e.evaluate(env)
    def free_vars(self): return self.e.free_vars()


_CMP: dict[str, Callable] = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
}


@dataclass(frozen=True)
class Cmp(Expr):
    op: str
    a: Expr
    b: Expr

    def to_z3(self, vars): return _CMP[self.op](self.a.to_z3(vars), self.b.to_z3(vars))
    def evaluate(self, env): return _CMP[self.op](self.a.evaluate(env), self.b.evaluate(env))
    def free_vars(self): return self.a.free_vars() | self.b.free_vars()


@dataclass(frozen=True)
class And(Expr):
    args: tuple[Expr, ...]

    def to_z3(self, vars): return z3.And([a.to_z3(vars) for a in self.args]) if self.args else z3.BoolVal(True)
    def evaluate(self, env): return all(a.evaluate(env) for a in self.args)
    def free_vars(self): return set().union(*(a.free_vars() for a in self.args)) if self.args else set()


@dataclass(frozen=True)
class Or(Expr):
    args: tuple[Expr, ...]

    def to_z3(self, vars): return z3.Or([a.to_z3(vars) for a in self.args]) if self.args else z3.BoolVal(False)
    def evaluate(self, env): return any(a.evaluate(env) for a in self.args)
    def free_vars(self): return set().union(*(a.free_vars() for a in self.args)) if self.args else set()


@dataclass(frozen=True)
class Not(Expr):
    e: Expr

    def to_z3(self, vars): return z3.Not(self.e.to_z3(vars))
    def evaluate(self, env): return not self.e.evaluate(env)
    def free_vars(self): return self.e.free_vars()


@dataclass(frozen=True)
class Implies(Expr):
    a: Expr
    b: Expr

    def to_z3(self, vars): return z3.Implies(self.a.to_z3(vars), self.b.to_z3(vars))
    def evaluate(self, env): return (not self.a.evaluate(env)) or bool(self.b.evaluate(env))
    def free_vars(self): return self.a.free_vars() | self.b.free_vars()


def all_of(*args: Expr) -> Expr:
    return And(tuple(args))


def any_of(*args: Expr) -> Expr:
    return Or(tuple(args))


def implies(a: Expr, b: Expr) -> Expr:
    return Implies(a, b)


def total(exprs) -> Expr:
    return Add(tuple(exprs))


def T() -> Expr:
    return BoolConst(True)
