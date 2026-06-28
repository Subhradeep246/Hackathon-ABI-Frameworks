# Production Gaps

The honest list of what this codebase is **not** yet doing that a real production deployment would need. Every gap has a "what we'd add" so the path is concrete, not hand-wavy.

| Gap | Status now | What we'd add for prod |
|---|---|---|
| **High availability** | Single Docker Compose stack on one laptop | RDS Multi-AZ Postgres, Redis Cluster, multi-region API replicas behind ALB, S3 for blobs |
| **AuthN/Z** | Stub `org_id` from env | OAuth/OIDC (Auth0/Cognito), RBAC per facility, audit log of who accessed what |
| **Secret management** | `.env` + `.env.example` | AWS Secrets Manager or HashiCorp Vault; periodic rotation of Baseten + 11Labs keys |
| **Distributed tracing** | structlog only | OpenTelemetry SDK → Tempo, traceid propagation across api/worker boundaries |
| **Alerting** | None | PagerDuty wired to Grafana SLOs (ingest lag, extraction failure rate, decision throughput) |
| **HIPAA / SOC 2** | Synthetic data only; PHI redaction layer (Presidio) **already in place** | BAAs with Baseten + 11Labs (or move to self-hosted LLM), encrypted-at-rest, audit log retention, redaction recall metrics, BAA with Postgres host |
| **Backups / DR** | None | RDS automated snapshots, cross-region replication, documented restore drill |
| **Cost guardrails** | LLM cache helps | Per-org rate limits, Baseten spend dashboards, fallback to smaller model on budget exhaustion |
| **Schema-drift detection on PCC API** | Manual | Pydantic strict mode + nightly contract test in CI against the real API |
| **Data retention / right to be forgotten** | None | TTL on raw_* tables, hard-delete API for patient_id, audit trail of deletions |
| **Multi-tenancy hardening** | `org_id` on every table | Row-level security in Postgres, per-org connection pools, per-org Prefect work pools |
| **Observability of LLM** | Local logs only | Per-call cost, latency, prompt/response logging (with redaction) to LLM-ops tooling like LangSmith or Helicone |
| **Self-healing on PCC schema change** | Pipeline crashes | Schema-version detection + alert + graceful degradation to last-known-good schema |

---

## Why these are deferred for the hackathon

The PRD's design intent is **production-ready patterns, not production infrastructure**. Every choice in the codebase (Postgres over DuckDB, Prefect over a script, Alembic migrations, multi-tenant `org_id` from day one, idempotent upserts, content-hash LLM cache) was made because it scales unchanged to production. The work above is the *infrastructure* layer that scales the team and the SLA — not the code path. We'd add it when there's a real SLA to defend.
