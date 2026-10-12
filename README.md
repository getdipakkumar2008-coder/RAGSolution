# RAGSolution

A Python Azure Functions RAG service that stores original files, document metadata,
text chunks, and native `VECTOR(1536)` embeddings together in **SQL Server 2025**.
No external vector store is used.

```text
PDF/image → Azure Function → Document Intelligence OCR → overlapping text chunks
          → Azure OpenAI embeddings → SQL Documents + Chunks
Question  → Azure OpenAI embedding → SQL cosine retrieval + metadata join
          → grounded Azure OpenAI chat response + file/chunk citations
```

## Prerequisites

- Python 3.12, Azure Functions Core Tools v4, and Microsoft ODBC Driver 18 for SQL Server.
- SQL Server 2025 (17.x), with a database reachable from the Function App.
- Azure AI Document Intelligence with the `prebuilt-read` model.
- Azure OpenAI deployments: **text-embedding-3-small**, with 1536 dimensions, and
  **gpt-4o** (or a chat deployment compatible with API `2024-10-21`, temperature,
  and `max_tokens`). Settings contain deployment names, not model IDs.
- An Azure Functions Python v2 hosting environment, storage account, and network
  access to all three services. This synchronous starter is intended for small
  files; HTTP responses are subject to the platform's approximately 230-second
  limit even though the function timeout is 10 minutes. Use queued/durable
  ingestion for larger or slower workloads.

## Setup and run

Run commands from the repository root. Create the database separately, then apply
`sql/schema.sql` to it using SQL Server Management Studio or `sqlcmd`:

```sh
sqlcmd -S YOUR-SERVER -d RAG -G -i sql/schema.sql
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp local.settings.example.json local.settings.json
```

Edit the ignored `local.settings.json` with real endpoints, deployment names, and
an encrypted SQL connection string. `DefaultAzureCredential` uses your local Azure
CLI login (`az login`) or the deployed app's managed identity. Grant that identity
**Cognitive Services OpenAI User** on OpenAI and **Cognitive Services User** on
Document Intelligence. Configure SQL Server's Microsoft Entra authentication and
create a database user for the identity, granting only `SELECT`/`INSERT` on
`dbo.Documents` and `dbo.Chunks`. Schema/index creation uses a separate administrator.
The sample SQL connection string deliberately omits UID, password, and
Authentication because the app supplies an Entra access token.

For SQL Server installations without Entra authentication, set
`SQL_USE_MANAGED_IDENTITY=false` and supply your supported ODBC authentication
configuration through protected application settings; keep TLS enabled and never
commit credentials. Azure AI services still use `DefaultAzureCredential`.

Start Azurite for `UseDevelopmentStorage=true`, or supply a real storage account
connection in local settings, then run:

```sh
func start
```

Publish to an existing Python Function App using
`func azure functionapp publish YOUR-FUNCTION-APP`. Configure the same `Values`
as Azure application settings (local settings are not published automatically),
enable managed identity, and ensure the deployed runtime has ODBC Driver 18
installed. Use a custom Functions image with that driver if your hosting image
does not include it.

## API

Deployed endpoints require a function key in `x-functions-key`. Local Core Tools
normally disables key authentication. Use HTTPS outside local development.

Upload a raw binary body, **not multipart**:

```sh
curl -X POST 'http://localhost:7071/api/documents?filename=report.pdf' \
  -H 'Content-Type: application/pdf' --data-binary @report.pdf
curl -X POST 'http://localhost:7071/api/query' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What does the report say about revenue?","top_k":5}'
```

Uploads accept PDF, JPEG, PNG, TIFF, BMP, and UTF-8 plain text. OCR handles PDFs
and images; text skips OCR. Each file is limited to 20 MiB and 128 chunks of 1500
characters with 200-character overlap. Embeddings are batched and validated before
an atomic database transaction. A SHA-256 content ID deduplicates identical files;
reuploads preserve the original filename and chunk IDs. Extracted text and the
original bytes remain in SQL alongside content type, embedding deployment, and
creation time. OCR/embedding failures leave no partial document rows.

Uploads return `{"document_id":"<sha256>","chunks":3}`. Queries accept a nonempty
question of up to 2000 characters and `top_k` from 1 to 20 (default 5), and return:

```json
{
  "answer": "Revenue increased [1].",
  "sources": [
    {
      "citation": 1,
      "document_id": "<sha256>",
      "filename": "report.pdf",
      "chunk": 0,
      "text": "Revenue increased.",
      "distance": 0.12
    }
  ]
}
```

Chunk ordinals are zero-based; citation IDs are one-based. Lower cosine distances
mean closer matches. An empty corpus returns a no-documents answer without a chat
call. Otherwise, the model is instructed to use only excerpts, cite them, ignore
embedded instructions, and abstain when unsupported. Model output is not a
guarantee of factual accuracy; callers should verify citations.
Invalid inputs return 400; service/configuration failures return a sanitized 503.

## SQL Server 2025 exact and ANN retrieval

**SQL Server 2025 is GA; its vector index and `VECTOR_SEARCH` capabilities are
still documented as preview.** The default `SQL_SEARCH_MODE=exact` uses native
`VECTOR_DISTANCE('cosine', ...)`, supports continuous ingestion, and needs no
preview switch. Both modes join retrieval results to file metadata in SQL and use
parameter binding, including JSON-to-`VECTOR(1536)` casts.

For a loaded, static corpus:

1. Pause uploads and apply `sql/enable_ann.sql` as an administrator. It enables
   `PREVIEW_FEATURES` and creates a cosine DiskANN vector index.
2. Set `SQL_SEARCH_MODE=ann` and restart the app. Queries use SQL Server 2025's
   `VECTOR_SEARCH(... TOP_N = 20)` syntax; uploads are rejected in this mode.
3. To ingest again, pause the app, run
   `DROP INDEX IX_Chunks_Embedding ON dbo.Chunks;`, switch back to `exact`, and
   restart. Rebuild the index after loading.

The SQL Server 2025 preview index requires a single integer clustered key
(provided by `Chunks.chunk_id`) and makes the indexed table **read-only**.
Do not apply newer cloud-only writable-index or `WITH APPROXIMATE` examples to
this implementation. ANN may return fewer results; deployment filtering is
applied after its 20 candidates. No claim of lower latency or better accuracy is
made without measuring a representative corpus.

Use the same embedding deployment/model for ingestion and querying. Retrieval
filters out other deployment names; do not change a deployment's underlying model
without clearing/re-embedding the corpus and rebuilding its index. Deployment
names alone cannot detect an underlying model replacement.

## Validation and security

```sh
python -m unittest discover -s tests -v
python -m pip check
```

Offline tests mock Azure/ODBC boundaries and exercise extraction, batching,
transaction rollback, deduplication, vector retrieval, grounding, and HTTP
validation. They do not prove server-side SQL/index support. For a live smoke
test, apply the schema, upload a text file and a scanned PDF/image, query a fact
unique to each file, verify the returned filename/text citation, reupload to check
deduplication, then freeze the corpus and repeat in ANN mode.

This is a **single trusted corpus**, not a multi-tenant authorization system.
Function keys grant access to all indexed knowledge; place user authentication,
authorization, request throttling, and document-level access controls in front
before exposing sensitive data or supporting multiple users/tenants. Store keys
in Key Vault/application settings, restrict network access, and encrypt backups.
Prompts treat retrieved text as untrusted, but prompt instructions alone do not
eliminate prompt injection. Responses include extracted source text, so authorize
callers for the whole corpus. The app avoids logging file contents, prompts,
credentials, and raw provider exceptions.

## References

- [SQL vector data type](https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type?view=sql-server-ver17)
- [CREATE VECTOR INDEX and preview restrictions](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-vector-index-transact-sql?view=sql-server-ver17)
- [VECTOR_SEARCH](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-search-transact-sql?view=sql-server-ver17)
- [VECTOR_DISTANCE](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-distance-transact-sql?view=sql-server-ver17)
- [Document Intelligence Read OCR](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0)