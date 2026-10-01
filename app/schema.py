from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _parse_money(value: object) -> object:
    # A float has already lost precision; callers must parse JSON with parse_float=Decimal.
    # Must be ValueError: pydantic only turns ValueError into a ValidationError, not TypeError.
    if isinstance(value, float):
        raise ValueError(  # noqa: TRY004
            f"monetary value must not be a float, received {value!r}; pass a str or Decimal"
        )
    # Invoices print amounts like "1,234.50"; Decimal() rejects the comma.
    if isinstance(value, str):
        return value.replace(",", "")
    return value


Money = Annotated[Decimal, BeforeValidator(_parse_money)]


class LineItem(BaseModel):
    description: str
    quantity: Money
    unit_price: Money
    line_total: Money
    vat_rate: Money
    vat_amount: Money


class Invoice(BaseModel):
    invoice_number: str | None = None
    invoice_date: date | None = None
    invoice_timestamp: datetime | None = None
    invoice_type: Literal["standard", "simplified", "unknown"]
    seller_name: str | None = None
    seller_vat_number: str | None = None
    buyer_name: str | None = None
    buyer_vat_number: str | None = None
    line_items: list[LineItem]
    subtotal: Money | None = None
    vat_total: Money | None = None
    total: Money | None = None
    currency: str = "SAR"


class FieldConfidence(BaseModel):
    field: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool


class Finding(BaseModel):
    rule: str
    severity: Literal["error", "warning"]
    message: str
    fields: list[str]


class CheckStatus(BaseModel):
    """One arithmetic check's result, or why it did not run. Rule names and line
    positions only, never amounts: this is what review_queue stores."""

    rule: str
    line: int | None  # 0-based line index for per-line checks, None for the totals
    outcome: Literal["pass", "fail", "not_checked"]
    reason: str | None = None  # set when not_checked


class CheckOutcome(CheckStatus):
    """A CheckStatus with its amounts, for the /extract response only. Nothing logs
    checks."""

    computed: Decimal | None = None  # what the other cells in the check add up to
    difference: Decimal | None = None  # the value read minus computed


class ExtractionResult(BaseModel):
    invoice: Invoice
    confidences: list[FieldConfidence]
    findings: list[Finding]
    status: Literal["ok", "needs_review"]
    checks: list[CheckOutcome]


class ReviewOutcome(BaseModel):
    """What /extract did about review: queued (with the reference to write on the
    paper invoice), or the resolver or queue failed."""

    status: Literal["queued", "error"]
    reference: str | None = None
    resolver_status: Literal["suggested", "ambiguous", "unresolvable"] | None = None


class ExtractResponse(ExtractionResult):
    review: ReviewOutcome | None  # None: no arithmetic finding, nothing to resolve


Edit = Literal[
    "known_substitution",
    "adjacent_swap",
    "digit_added",
    "digit_dropped",
    "other_substitution",
    "other",
]


class Candidate(BaseModel):
    """A value one cell would need for the invoice arithmetic to pass. A suggestion
    for a person to accept or reject; never applied automatically."""

    field: str
    read_value: Decimal
    value: Decimal
    edit: Edit  # how value differs from read_value, digit by digit
    rank: int


class Reading(BaseModel):
    """A value as the model read it, for a cell in a failed check."""

    field: str
    read_value: Decimal


class Resolution(BaseModel):
    status: Literal["not_needed", "suggested", "ambiguous", "unresolvable"]
    # reason and failed_checks name fields and checks, never amounts: they are logged.
    reason: str
    failed_checks: list[str]
    candidates: list[Candidate]
    # Every cell in the failed checks as read: what a reviewer checks against paper.
    involved: list[Reading] = []


class ReviewItem(BaseModel):
    """A pending review. Amounts and field paths only: no names, VAT numbers,
    descriptions or images."""

    id: str
    reference: str  # short, e.g. R-0042, for writing on the paper invoice
    created_utc: str
    audit_id: str
    image_sha256: str
    status: Literal["suggested", "ambiguous", "unresolvable"]
    reason: str
    failed_checks: list[str]
    candidates: list[Candidate]
    involved: list[Reading]
    checks: list[CheckStatus] | None  # None: queued before check outcomes were stored


class CallMetadata(BaseModel):
    model: str
    prompt_version: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal | None
    cache_hit: bool
    temperature_zero: bool
