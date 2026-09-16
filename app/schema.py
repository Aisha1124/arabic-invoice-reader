from datetime import date
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


class ExtractionResult(BaseModel):
    invoice: Invoice
    confidences: list[FieldConfidence]
    findings: list[Finding]
    status: Literal["ok", "needs_review"]
