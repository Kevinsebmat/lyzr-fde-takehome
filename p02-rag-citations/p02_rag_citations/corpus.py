"""The knowledge base and how it gets chunked.

A deliberately *incomplete* corpus. Most RAG demos pick questions the corpus
answers well, which proves nothing — the whole point of citation grounding is
what the system does at the edges. This corpus covers refunds, SLA, retention,
SSO and rate limits, and deliberately says nothing about HIPAA, on-premise
deployment or pricing tiers, so the refusal path can be demonstrated rather
than described.

Chunking is paragraph-based with a heading prefix carried into every chunk.
Naive fixed-width chunking splits a policy mid-sentence and then cites half a
rule, which is exactly the failure that makes citations untrustworthy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COLLECTION = "northwind_kb"


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    source: str
    body: str


DOCUMENTS: tuple[Document, ...] = (
    Document(
        doc_id="refunds",
        title="Refund and Credit Policy",
        source="handbook/billing/refunds.md",
        body="""## Standard refund window

Customers on monthly plans may request a full refund within 14 days of a
charge. Requests after 14 days are declined automatically by billing.

## Annual plans

Annual plans are refundable on a pro-rata basis for the unused remainder of
the term, minus a 10% administrative fee. The fee is waived if the customer is
migrating to a higher tier.

## Service credits for downtime

When monthly uptime falls below the committed 99.9%, affected customers
receive a service credit calculated as 10% of the monthly fee for each full
0.1% below target, capped at 50% of the monthly fee. Credits are applied to
the next invoice and are never paid out in cash.

## Who can approve exceptions

Refunds above $10,000 require written approval from the VP of Finance.
Support engineers cannot approve any refund above $500.""",
    ),
    Document(
        doc_id="sla",
        title="Service Level Agreement",
        source="handbook/legal/sla.md",
        body="""## Uptime commitment

Northwind Cloud commits to 99.9% monthly uptime for the API and dashboard on
Business and Enterprise plans. Starter plans carry no uptime commitment.

## Measurement

Uptime is measured in one-minute intervals from three independent external
probes. A minute counts as down only when at least two of three probes fail.
Scheduled maintenance announced 72 hours in advance is excluded.

## Support response targets

Enterprise: 1 hour for critical, 4 hours for high, one business day otherwise.
Business: 4 hours for critical, one business day otherwise. Starter: best
effort, no target.

## Exclusions

The SLA does not cover failures caused by customer misconfiguration, traffic
exceeding published rate limits, or third-party network outages outside our
infrastructure.""",
    ),
    Document(
        doc_id="retention",
        title="Data Retention and Deletion",
        source="handbook/security/retention.md",
        body="""## Default retention

Application logs are retained for 30 days. Audit logs are retained for 400
days. Customer content is retained until the customer deletes it or closes
the account.

## Deletion on account closure

When an account is closed, customer content is soft-deleted immediately and
permanently purged after 30 days. During the 30-day window a customer may
request full restoration at no charge.

## Backups

Encrypted backups are held for 35 days. A deletion request removes data from
live systems immediately; backup copies age out within the 35-day cycle and
are not selectively purged.

## Data residency

Customers may pin data to the EU or US region at account creation. The region
cannot be changed after creation without a full account migration.""",
    ),
    Document(
        doc_id="sso",
        title="SSO and Directory Sync",
        source="handbook/product/sso.md",
        body="""## Supported protocols

SAML 2.0 and OIDC are supported on Business and Enterprise plans. SCIM 2.0
directory sync is Enterprise only.

## Setup

An administrator uploads the identity provider metadata XML, maps the email
and groups claims, then runs a test assertion before enabling enforcement.
Enabling enforcement immediately signs out all existing sessions.

## Just-in-time provisioning

When JIT is enabled, a user authenticating for the first time is created
automatically and assigned the role mapped from their directory group. Users
removed from the directory are deactivated within one SCIM sync cycle, which
runs every four hours.

## Break-glass access

Every organisation must keep at least one password-based administrator account
exempt from SSO enforcement, so that an identity provider outage cannot lock
the organisation out.""",
    ),
    Document(
        doc_id="ratelimits",
        title="API Rate Limits",
        source="handbook/product/rate-limits.md",
        body="""## Published limits

Starter allows 60 requests per minute. Business allows 600 requests per
minute. Enterprise limits are set per contract and default to 3,000 requests
per minute.

## Burst behaviour

A token bucket allows short bursts up to twice the sustained limit for a
maximum of ten seconds, after which requests receive HTTP 429 with a
Retry-After header.

## Raising a limit

Limit increases on Business require a support request and take effect within
one business day. Enterprise limit changes are handled by the account team as
a contract amendment.""",
    ),
)


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    title: str
    source: str
    heading: str

    def as_document(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "title": self.title,
            "source": self.source,
            "metadata": {"heading": self.heading},
        }


def chunk_document(doc: Document) -> list[Chunk]:
    """Split on `##` headings, keeping each section whole.

    The heading is prefixed into the chunk text so an embedding of "credits for
    downtime" is close to the section that discusses it, and so a citation
    displayed to a user carries the context it came from.
    """
    chunks: list[Chunk] = []
    sections = re.split(r"\n(?=## )", doc.body.strip())
    for i, section in enumerate(sections):
        lines = section.strip().splitlines()
        heading = lines[0].lstrip("# ").strip() if lines[0].startswith("## ") else doc.title
        body = "\n".join(lines[1:] if lines[0].startswith("## ") else lines).strip()
        if not body:
            continue
        chunks.append(
            Chunk(
                id=f"{doc.doc_id}#{i}",
                text=f"{doc.title} — {heading}\n\n{body}",
                title=doc.title,
                source=f"{doc.source}#{_slug(heading)}",
                heading=heading,
            )
        )
    return chunks


def all_chunks() -> list[Chunk]:
    return [c for doc in DOCUMENTS for c in chunk_document(doc)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


#: Questions the corpus can answer, and questions it deliberately cannot.
#: The second list is the interesting one.
ANSWERABLE = (
    "How long do customers have to request a refund on a monthly plan?",
    "What uptime do we commit to and which plans does it cover?",
    "How long are audit logs kept?",
    "Which SSO protocols work on the Business plan?",
    "What happens when a customer exceeds their rate limit?",
)

UNANSWERABLE = (
    "Do you sign a HIPAA business associate agreement?",
    "What does the Enterprise plan cost per seat per year?",
    "Can Northwind Cloud be deployed on-premise in our own datacentre?",
    "What is the CEO's direct phone number?",
)
