# RAGSolution — Specification

## 1. Purpose
A serverless RAG pipeline that ingests documents (scanned/text), extracts and embeds their content, stores vectors natively in Azure SQL, and answers user questions with citation-grounded responses from Azure OpenAI.

See `docs/ARCHITECTURE.md` for the architecture diagram and workflow sequences.

## 2. Components

| Component | Technology | Responsibility |
|---|---|---|
| Compute | Azure Functions (Python, `FUNCTIONS_WORKER_RUNTIME=python`) | HTTP-triggered ingest/ask endpoints, `routePrefix=/api` |
| OCR/Extraction | Azure AI Document Intelligence (`prebuilt-read`) | Extracts text from PDF/image uploads |
| Embeddings | Azure OpenAI (`AZURE_OPENAI_EMBEDDING_DEPLOYMENT`) | 1536-dim embeddings, batch size 16 |
| Generation | Azure OpenAI (`AZURE_OPENAI_CHAT_DEPLOYMENT`) | Grounded, cited answer synthesis, temp=0 |
| Storage | Azure SQL Database | `dbo.Documents` + `dbo.Chunks` with native `VECTOR(1536)` column |
| Identity | Microsoft Entra ID (`DefaultAzureCredential`) | Token-based auth to OpenAI, Document Intelligence, and optionally SQL |
| Driver | `pyodbc` + ODBC Driver 18 | SQL connectivity, `Encrypt=yes` |

## 3. Data Model

### dbo.Documents
- `document_id VARCHAR(64) PK` (sha256 hex digest, content-addressed)
- `filename NVARCHAR(255)`
- `content_type VARCHAR(64)`
- `binary_content VARBINARY(MAX)`
- `embedding_model NVARCHAR(128)`
- `created_at DATETIME2` (default `SYSUTCDATETIME()`)

### dbo.Chunks
- `chunk_id INT IDENTITY PK`
- `document_id VARCHAR(64) FK -> Documents`
- `ordinal INT`
- `content NVARCHAR(MAX)`
- `embedding VECTOR(1536)`
- Unique constraint `(document_id, ordinal)`

### Optional Index
- `IX_Chunks_Embedding` — DiskANN, cosine metric (preview feature; makes table read-only for inserts). Created via `sql/enable_ann.sql`.

## 4. API Behavior

### Ingest
- Input: binary file content, filename, content_type.
- Constraints: 1 byte – 20 MiB; filename 1–255 chars; content_type in `{application/pdf, image/jpeg, image/png, image/tiff, image/bmp, text/plain}`.
- Disabled entirely when `SQL_SEARCH_MODE=ann`.
- Output: `{document_id, chunks}`.
- Idempotent: identical binary content (same sha256) is a no-op; same hash with a different embedding model raises an error.

### Ask
- Input: `question` (string, 1–2000 chars), `top_k` (int, 1–20, default 5).
- Output: `{answer, sources: [{citation, document_id, filename, chunk, text, distance}]}`.
- Returns `"No indexed documents were found."` with empty sources when nothing matches.

## 5. Constraints & Limits
- Max upload size: 20 MiB (`MAX_FILE_BYTES`); also enforced at `host.json` level via `FUNCTIONS_REQUEST_BODY_SIZE_LIMIT=20971520`.
- Max 128 chunks per document (`MAX_CHUNKS`).
- Chunking: 1500 chars with 200 overlap.
- Embedding batch size: 16 (API limit mitigation).
- Function timeout: 10 minutes (`host.json`); concurrency capped at `maxConcurrentRequests=4`, `maxOutstandingRequests=20`.

## 6. Security Considerations
- Managed identity–first design (no static secrets in config beyond endpoints/connection string).
- Prompt-injection mitigation: system prompt explicitly instructs the model to treat retrieved source text as untrusted data, not instructions.
- Dedupe via content hash (sha256) prevents duplicate storage and conflicting re-embeddings.
- `embedding_model` filter in search queries prevents comparing vectors from different embedding models/dimensionality drift.
- SQL transactions use `UPDLOCK, HOLDLOCK` to avoid race conditions on concurrent ingestion of the same document.

## 7. Known Operational Trade-off
Switching to ANN mode (`SQL_SEARCH_MODE=ann`) via `sql/enable_ann.sql` disables ingestion entirely (enforced in `RagService.ingest`), since the DiskANN vector index in SQL Server 2025 preview requires the table to be read-only. This is a deliberate operational gate — teams must pause ingestion before enabling ANN, and must drop the index to resume ingestion.

## 8. Configuration (local.settings.example.json)
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_CHAT_DEPLOYMENT`
- `DOCUMENT_INTELLIGENCE_ENDPOINT`
- `SQL_CONNECTION_STRING`, `SQL_USE_MANAGED_IDENTITY` (true/false), `SQL_SEARCH_MODE` (exact/ann)
- `FUNCTIONS_REQUEST_BODY_SIZE_LIMIT`

## 9. Dependencies (requirements.txt)
```
azure-functions==1.25.0
azure-identity==1.26.0
azure-ai-documentintelligence==1.0.2
openai==3.29.0
pyodbc==5.3.0
```
