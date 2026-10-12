SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.Documents', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Documents
    (
        document_id VARCHAR(64) NOT NULL
            CONSTRAINT PK_Documents PRIMARY KEY,
        filename NVARCHAR(255) NOT NULL,
        content_type VARCHAR(64) NOT NULL,
        binary_content VARBINARY(MAX) NOT NULL,
        embedding_model NVARCHAR(128) NOT NULL,
        created_at DATETIME2 NOT NULL
            CONSTRAINT DF_Documents_CreatedAt DEFAULT SYSUTCDATETIME()
    );
END;

IF OBJECT_ID(N'dbo.Chunks', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Chunks
    (
        chunk_id INT IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_Chunks PRIMARY KEY CLUSTERED,
        document_id VARCHAR(64) NOT NULL
            CONSTRAINT FK_Chunks_Documents REFERENCES dbo.Documents(document_id),
        ordinal INT NOT NULL,
        content NVARCHAR(MAX) NOT NULL,
        embedding VECTOR(1536) NOT NULL,
        CONSTRAINT UQ_Chunks_DocumentOrdinal UNIQUE(document_id, ordinal)
    );
END;

COMMIT;
