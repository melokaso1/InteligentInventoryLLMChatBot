from typing import Any

from pydantic import BaseModel, Field


class ChatMessageRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    message: str
    state: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class OperationSummary(BaseModel):
    transaction_id: str = Field(alias="transactionId")
    status: str
    product_code: str = Field(alias="productCode")
    product_name: str = Field(alias="productName")
    quantity: int
    unit_price: float = Field(alias="unitPrice")
    subtotal: float
    tax: float
    total: float

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class ChatMessageResponse(BaseModel):
    response: str
    state: str
    state_json: dict[str, Any] | None = Field(default=None, alias="stateJson")
    invoice_number: str | None = Field(default=None, alias="invoiceNumber")
    chips: list[str] | None = None
    operation_summary: OperationSummary | None = Field(default=None, alias="operationSummary")

    model_config = {"populate_by_name": True, "serialize_by_alias": True}
