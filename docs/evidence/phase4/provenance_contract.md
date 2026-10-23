# Structured Proposal Provenance Contract

This document specifies the interface, data model, and database query contract for retrieving proposal status provenance (reasons, event types, and timestamps for terminal states like quarantine, rejection, or supersession) without executing database schema migrations.

## 1. Data Model & Interface Signatures

### Python Dataclass
The structured payload is represented as a queryable dataclass in [proposal_service.py](packages/aether/services/proposal_service.py):

```python
@dataclass
class ProposalProvenance:
    proposal_id: str
    event_type: str
    event_reason: Optional[str]
    created_at: datetime
```

### Service Interface
The service-level retrieval method signature:
```python
def get_proposal_provenance(self, proposal_id: str) -> Optional[ProposalProvenance]:
    """
    Retrieve the structured provenance details from SystemMetricEvent for terminal statuses.
    Uses a join on SystemMetricEvent to fetch the details without schema migrations.
    """
```

### HTTP REST API Endpoint Route
Exposed via the GET route in [routes.py](packages/aether/api/routes.py):
```http
GET /v1/repos/{repo_id}/proposals/{proposal_id}/provenance
```
**Response JSON Schema:**
```json
{
  "proposal_id": "string",
  "event_type": "string" or null,
  "event_reason": "string" or null,
  "created_at": "ISO-8601 string" or null
}
```

## 2. Database Join Contract

To preserve structural database stability, the details are retrieved via a JOIN against the `aether_system_metric_events` table instead of storing them directly on `aether_proposals`.

### SQL Equivalent Query

```sql
SELECT sme.proposal_id, sme.event_type, sme.event_reason, sme.created_at
FROM aether_system_metric_events sme
JOIN aether_proposals p ON p.id = sme.proposal_id
WHERE p.id = :proposal_id
  AND sme.event_type IN ('status.rejected', 'status.quarantined', 'status.superseded')
ORDER BY sme.created_at DESC
LIMIT 1;
```

## 3. Supported Status & Conflict Mappings

The following table maps the `ProposalStatus` terminal values and blueprint designs to the corresponding `SystemMetricEventType` records scanned by the join:

| Source / Proposal Status | Blueprint design status | Shipped System Event Type | Expected Event Reason Source |
|--------------------------|--------------------------|---------------------------|------------------------------|
| `REJECTED`               | -                        | `STATUS_REJECTED`         | e.g. `"gate_failed_compilation"`, `"non_positive_score"` |
| `QUARANTINED`            | -                        | `STATUS_QUARANTINED`      | e.g. `"oracle_insufficiency_escalated"` |
| `SUPERSEDED`             | -                        | `STATUS_SUPERSEDED`       | e.g. `"Superseded by prop_xxxx"` |
| `REJECTED` (sibling)     | `SUPERSEDED_SEMANTIC_CONFLICT` | `STATUS_REJECTED`         | `"conflict_sibling_failed_gates"` |
| `QUARANTINED` (sibling)  | `SUPERSEDED_SEMANTIC_CONFLICT` | `STATUS_QUARANTINED`      | `"conflict_sibling_failed_oracle_sufficiency"` |

## 4. Test Coverage & Engine Coherence

The interface is validated under both SQLite (in-memory) and PostgreSQL to guarantee dialectical coherence and prevent transaction leaks:
- **SQLite & REST API validation suite**: [tests/aether/test_provenance.py](tests/aether/test_provenance.py)
- **PostgreSQL validation suite**: [tests/aether/test_postgres_concurrency.py](tests/aether/test_postgres_concurrency.py)

