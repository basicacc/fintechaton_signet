"""Domain models: clients, products, proposals.

Amounts are whole RMB (int). Risk levels are 1..5 on both the client (C1..C5)
and product (R1..R5) scales, as required by NFRA Art. 22 and Art. 29.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Capacity(StrEnum):
    FULL = "full"
    LIMITED = "limited"  # 限制民事行为能力
    NONE = "none"  # 无民事行为能力


class InvestorType(StrEnum):
    RETAIL = "retail"  # 普通投资者 (Art. 28)
    PROFESSIONAL = "professional"  # 专业投资者 (Art. 27)


class ProductType(StrEnum):
    DEPOSIT = "deposit"
    WEALTH = "wealth"  # bank wealth-management product 理财
    FUND = "fund"
    BOND = "bond"
    INSURANCE_INVESTMENT = "insurance_investment"


class Market(StrEnum):
    CN = "CN"  # sold by a mainland bank
    HK = "HK"  # sold by a Hong Kong bank (e.g. via WMC Southbound)


class Product(BaseModel):
    id: str
    name: str
    name_zh: str = ""
    aliases: list[str] = Field(default_factory=list)  # other ways clients and the model refer to it
    type: ProductType
    market: Market
    issuer_risk: int = Field(ge=1, le=5)
    distributor_risk: int = Field(ge=1, le=5)
    currency: str = "CNY"
    min_investment: int = Field(ge=0, default=0)
    liquidity_days: int = Field(ge=0, default=0)  # days to get cash back
    complex: bool = False  # SFC "complex product"
    private: bool = False  # 私募 (Art. 30)
    wmc_eligible: bool = False  # on the Southbound eligible list
    annual_fee_bps: int = 0
    returns_pct: dict[str, float] = Field(default_factory=dict)  # {"2023": 3.1, ...}
    max_drawdown_pct: float | None = None
    description: str = ""

    @property
    def effective_risk(self) -> int:
        # NFRA Art. 22: if issuer and distributor ratings differ, use the higher one.
        return max(self.issuer_risk, self.distributor_risk)

    @property
    def is_cash_like(self) -> bool:
        return self.effective_risk == 1 and self.liquidity_days <= 1


class Assessment(BaseModel):
    level: int = Field(ge=1, le=5)
    at: datetime


class Client(BaseModel):
    id: str
    age: int = Field(ge=0, le=130)
    capacity: Capacity = Capacity.FULL
    guardian_consent: bool = False
    investor_type: InvestorType = InvestorType.RETAIL
    assessments: list[Assessment] = Field(default_factory=list)  # oldest first
    investable_assets: int = Field(ge=0)  # liquid financial assets available to invest
    monthly_expenses: int = Field(ge=0, default=0)
    annual_income: int = Field(ge=0, default=0)
    horizon_years: int = Field(ge=0, default=1)
    qualified_private: bool = False  # meets private-product investor standard (Art. 30)
    # Wealth Management Connect (Southbound) facts
    gba_resident: bool = False
    investment_experience_years: int = Field(ge=0, default=0)
    net_household_financial_assets: int = Field(ge=0, default=0)
    household_financial_assets: int = Field(ge=0, default=0)
    min_annual_income_3y: int = Field(ge=0, default=0)  # average annual personal income over the last 3 years
    wmc_quota_used: int = Field(ge=0, default=0)
    holdings: dict[str, int] = Field(default_factory=dict)  # product_id -> amount
    reported_change: bool = False  # client reported a change affecting risk tolerance (Art. 29)
    profile_flags: list[str] = Field(default_factory=list)  # e.g. integrity contradictions
    level_cap: int | None = Field(default=None, ge=1, le=5)  # conservative cap pending human review

    @property
    def current_assessment(self) -> Assessment | None:
        return self.assessments[-1] if self.assessments else None

    def assessment_valid_at(self, when: datetime) -> bool:
        a = self.current_assessment
        if a is None or self.reported_change:
            return False
        # Art. 29: validity is in principle 12 months.
        return when - a.at <= timedelta(days=365)


class Procedure(BaseModel):
    """Procedural facts the gateway checks before execution."""

    disclosures_shown: set[str] = Field(default_factory=set)
    enhanced_warning: bool = False
    consideration_hours: int = 0
    follow_up_scheduled: bool = False
    client_confirmed: bool = False
    level_change_confirmed: bool = False  # Art. 29: re-confirm when level differs from last time


DISCLOSURE_ITEMS = (
    "basic_info",  # Art. 31(1)
    "risk_level",  # Art. 31(2), incl. that it may change
    "principal_loss",  # Art. 31(3)
    "fees",  # Art. 31(4)
    "matching_opinion",  # Art. 31(5)
)


class Proposal(BaseModel):
    client_id: str
    purchases: dict[str, int]  # product_id -> amount (whole RMB)
    as_of: datetime
    procedure: Procedure = Field(default_factory=Procedure)
    message: str = ""  # advisor's natural-language explanation (checked separately)

    @model_validator(mode="after")
    def _non_negative(self):
        for pid, amt in self.purchases.items():
            if amt < 0:
                raise ValueError(f"negative amount for {pid}")
        return self
