"""Domain schemas for the extraction demo.

Support-ticket triage, chosen because it exercises everything that actually
breaks in production structured output: a constrained float, a bounded integer,
enums, a nested list of objects, and an optional field the model likes to
hallucinate a value into.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class Category(str, Enum):
    billing = "billing"
    outage = "outage"
    bug = "bug"
    feature_request = "feature_request"
    account = "account"
    other = "other"


class ActionItem(BaseModel):
    """Nested object — the shape models most often flatten or drop."""

    description: str = Field(min_length=3, max_length=200)
    owner_team: str = Field(min_length=2, max_length=40)
    due_within_hours: int = Field(ge=1, le=720)


class TicketTriage(BaseModel):
    """The extraction target.

    `model_config` forbids extras deliberately: a model that invents a field is
    telling you the prompt and the schema disagree, and silently dropping it
    hides that.
    """

    model_config = {"extra": "forbid"}

    severity: Severity
    category: Category
    summary: str = Field(min_length=10, max_length=300)
    customer_sentiment: float = Field(
        ge=-1.0, le=1.0, description="-1 furious, 0 neutral, 1 delighted"
    )
    affected_users: int = Field(ge=0)
    action_items: list[ActionItem] = Field(min_length=1, max_length=5)
    requires_human_review: bool
    refund_amount_usd: float | None = Field(
        default=None, ge=0, description="Only when the customer explicitly asks for money back"
    )

    @field_validator("summary")
    @classmethod
    def summary_is_not_an_echo(cls, v: str) -> str:
        """A cross-field business rule, not a type check.

        Type-valid but useless output is the failure mode schema validation
        alone does not catch, so the repair loop has to handle it too.
        """
        if v.strip().lower().startswith(("the ticket", "this ticket", "the customer says")):
            raise ValueError(
                "summary must state the problem directly, not narrate the ticket "
                "(do not begin with 'the ticket'/'this ticket'/'the customer says')"
            )
        return v


# ---------- tool contracts ----------


class LookupAccountInput(BaseModel):
    model_config = {"extra": "forbid"}

    account_id: str = Field(pattern=r"^ACC-\d{4,8}$")


class LookupAccountOutput(BaseModel):
    """The tool's side of the contract.

    Validating what a tool *returns* is separate from validating what the model
    emits: a violation here is a bug in your integration, not a bad completion,
    and conflating the two sends you debugging the wrong half of the system.
    """

    account_id: str
    plan: str
    monthly_spend_usd: float = Field(ge=0)
    open_tickets: int = Field(ge=0)
    is_enterprise: bool
