# RAGSolution — Foundry Parallel Prototype Plan & Migration Checklist

> **Status:** Proposal / not implemented. Companion to `docs/Future_ARCHITECTURE-FOUNDRY.md`. Describes how to run the current Azure SQL-based RAG (`rag.py`) and the Azure AI Foundry variant **in parallel**, so the business can compare them on real data before deciding which to adopt.

## 1. Guiding Principles
- **No disruption to current prod path** — `rag.py` keeps serving all real traffic; Foundry is a shadow/parallel path.
- **Same inputs, same questions** — both paths must ingest the same documents and answer the same test questions so comparison is apples-to-apples.
- **Decision gate, not open-ended** — define success metrics up front and a fixed evaluation window (e.g., 4–6 weeks).

## 2. Prototype Plan (build the parallel path)

### Phase 0 — Scaffolding (~2–3 days)
- Create a feature flag / config switch, e.g. `RAG_BACKEND = "sql" | "foundry" | "both"`.
- Add a new module `foundry_rag.py` alongside `rag.py` — same interface (`ingest()`, `ask()`) so callers don't care which backend answers.
- Stand up an Azure AI Foundry project + hub, Azure AI Search resource, and a "Add your data" data source (blob container) in a **non-prod resource group**.

### Phase 1 — Parallel Ingestion (~1 week)
- On every `POST /ingest` call, after writing to Azure SQL as today, **also** push the raw file to the Foundry data source container (fire-and-forget or queued via Storage Queue/Service Bus so it doesn't block/slow the existing response).
- Preserve your own dedupe ledger (sha256) as a gatekeeper **before** forwarding to Foundry, since Foundry has no built-in equivalent — this avoids double-ingesting the same file into Search.
- Log `document_id`, ingestion timestamp, and Foundry ingestion status (success/failure) to a lightweight tracking table (`dbo.FoundryIngestionLog`) so you can audit parity between the two stores.

### Phase 2 — Parallel Query (~1 week)
- Add a new `POST /ask?backend=foundry` (or header-based routing) endpoint that calls the Foundry Agent / File Search tool instead of `SqlStore.search()`.
- For a subset of real or synthetic questions, call **both** backends and log results side by side (don't show Foundry answers to end users yet — shadow mode only).
- Capture: answer text, citations, latency, token usage/cost, retrieval top_k relevance.

### Phase 3 — Evaluation Harness (~1 week)
- Build a small eval script with a fixed question set (ideally 30–50 representative Qs covering edge cases: ambiguous phrasing, multi-doc synthesis, out-of-scope questions that should be refused).
- For each question, record for both backends:
  - Answer correctness (manual or LLM-graded against a gold answer)
  - Citation accuracy (do cited chunks actually support the claim?)
  - Latency (p50/p95)
  - Cost per query (OpenAI + Search/SQL compute)
  - Prompt-injection resistance (reuse/adapt existing red-team prompts against both)

### Phase 4 — Business Review
- Package results into a comparison scorecard (see checklist below) and present to stakeholders for a go/no-go or hybrid decision.

## 3. Migration / Comparison Checklist

### Infrastructure
- [ ] Foundry project + hub provisioned (separate resource group, non-prod subscription/tier if possible)
- [ ] Azure AI Search index created and configured for hybrid vector+keyword search
- [ ] Data source connector (blob container) wired to Foundry "Add your data"
- [ ] Foundry Agent configured with File Search/Grounding tool + chat model deployment
- [ ] Managed Identity granted access to: Search, Document Intelligence, storage, (optionally) SQL
- [ ] Cost alerts/budgets set on the new Search + Foundry resources

### Ingestion parity
- [ ] Dedupe ledger still enforced before forwarding to Foundry (sha256 check)
- [ ] Embedding-model-mismatch protection re-implemented or consciously waived
- [ ] File size/type/filename validation applied identically to both paths
- [ ] Ingestion status reconciliation log/report (SQL vs Foundry success rates)
- [ ] Chunking config in Foundry tuned to approximate 1500/200/128-cap behavior, or documented deviation

### Query parity
- [ ] Shadow-mode `/ask` endpoint hitting Foundry Agent
- [ ] Citation format mapped to existing `{citation, document_id, filename, chunk, text, distance}` shape (or equivalent)
- [ ] top_k behavior validated against current 1–20 range
- [ ] Prompt-injection test suite run against Foundry's default grounding instructions

### Evaluation metrics (scorecard)
| Metric | Current (SQL) | Foundry | Winner |
|---|---|---|---|
| Answer accuracy (%) | | | |
| Citation precision | | | |
| p95 latency (ingest) | | | |
| p95 latency (query) | | | |
| Cost per 1k ingested docs | | | |
| Cost per 1k queries | | | |
| Prompt-injection pass rate | | | |
| Ops effort (monitoring/alerts/reconciliation) | | | |
| Concurrent read/write during ingestion | N/A (ANN lock) | Yes | Foundry |
| Dedupe/compliance control | Strong | Needs rebuild | SQL |

### Decision criteria
- [ ] Define minimum bar for Foundry to "win" per metric category (e.g., accuracy within 2%, cost within 1.5x, no regression on prompt-injection)
- [ ] Define rollback plan if Foundry underperforms (simply stop forwarding ingestion, keep SQL as sole backend)
- [ ] Define hybrid option explicitly: e.g., keep SQL for compliance/audit of raw content, but let business choose which system serves live queries

## 4. Suggested Timeline
| Week | Activity |
|---|---|
| 1 | Provision Foundry resources, scaffold config/interfaces |
| 2 | Parallel ingestion pipeline live (shadow writes) |
| 3 | Parallel query shadow mode + logging |
| 4 | Run eval harness across fixed question set |
| 5 | Compile scorecard, cost report, security review |
| 6 | Business review meeting → go/no-go/hybrid decision |
