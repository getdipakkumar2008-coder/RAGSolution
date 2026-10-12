-- SQL Server 2025 preview indexes make Chunks read-only. Pause ingestion first.
ALTER DATABASE SCOPED CONFIGURATION SET PREVIEW_FEATURES = ON;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.Chunks') AND name = N'IX_Chunks_Embedding'
)
BEGIN
    CREATE VECTOR INDEX IX_Chunks_Embedding
    ON dbo.Chunks(embedding)
    WITH (METRIC = 'cosine', TYPE = 'DiskANN');
END;
GO
