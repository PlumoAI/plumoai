#!/bin/sh
# PostgreSQL initialization script for PlumoAI
# Runs on first container startup (fresh postgres_data volume)
set -e

echo "🐘 Initializing PostgreSQL for PlumoAI..."

# Use environment variables set by postgres image (from POSTGRES_DB and POSTGRES_USER)
# These are set automatically when using POSTGRES_DB/POSTGRES_USER env vars
PSQL_USER="${POSTGRES_USER:-plumoai_user}"
PSQL_DB="${POSTGRES_DB:-plumoai}"

# Create the application database if it doesn't exist
psql -v ON_ERROR_STOP=1 --username "$PSQL_USER" --dbname "$PSQL_DB" <<-EOSQL
    -- Enable pgvector extension for vector search
    CREATE EXTENSION IF NOT EXISTS vector;

    -- ============================================
    -- RELATIONAL TABLES (MySQL replacement)
    -- ============================================

    -- Users table
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        password_hash TEXT,
        role TEXT DEFAULT 'user',
        company_id TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    -- Projects table
    CREATE TABLE IF NOT EXISTS projects (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        company_id TEXT NOT NULL,
        owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        status TEXT DEFAULT 'active',
        config JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company_id);
    CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id);

    -- Tasks table
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        description TEXT,
        assigned_to TEXT,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to);

    -- Execution logs table
    CREATE TABLE IF NOT EXISTS execution_logs (
        id SERIAL PRIMARY KEY,
        agent_id TEXT NOT NULL,
        user_id INTEGER,
        company_id TEXT,
        session_id TEXT,
        operation TEXT,
        input_data JSONB DEFAULT '{}',
        output_data JSONB DEFAULT '{}',
        status TEXT DEFAULT 'success',
        duration_ms INTEGER,
        error_message TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_exec_logs_agent ON execution_logs(agent_id);
    CREATE INDEX IF NOT EXISTS idx_exec_logs_session ON execution_logs(session_id);

    -- Auth sessions table
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(token_hash);
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

    -- ============================================
    -- DOCUMENT STORE TABLES (MongoDB replacement)
    -- ============================================

    -- Agent states (dynamic, schema-less agent configuration)
    -- user_id is TEXT because it can be an external agent ID, not necessarily a users.id
    CREATE TABLE IF NOT EXISTS agent_states (
        id SERIAL PRIMARY KEY,
        agent_id TEXT NOT NULL,
        user_id TEXT,
        company_id TEXT,
        state JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_agent_states_agent ON agent_states(agent_id);
    CREATE INDEX IF NOT EXISTS idx_agent_states_user ON agent_states(user_id);

    -- Tool outputs (execution results, dynamic schema)
    CREATE TABLE IF NOT EXISTS tool_outputs (
        id SERIAL PRIMARY KEY,
        execution_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        output JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_tool_outputs_execution ON tool_outputs(execution_id);

    -- Execution metadata (unstructured execution context)
    CREATE TABLE IF NOT EXISTS execution_metadata (
        id SERIAL PRIMARY KEY,
        execution_id TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_exec_metadata_execution ON execution_metadata(execution_id);

    -- ============================================
    -- VECTOR SEARCH TABLES (Milvus replacement)
    -- ============================================

    -- Knowledgebase documents
    CREATE TABLE IF NOT EXISTS knowledgebase_documents (
        id SERIAL PRIMARY KEY,
        company_id TEXT NOT NULL,
        project_fid TEXT,
        title TEXT NOT NULL,
        file_type TEXT,
        source_path TEXT,
        doc_type TEXT DEFAULT 'general',
        language TEXT DEFAULT 'en',
        chunk_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_kb_docs_company ON knowledgebase_documents(company_id);
    CREATE INDEX IF NOT EXISTS idx_kb_docs_project ON knowledgebase_documents(project_fid);
    CREATE INDEX IF NOT EXISTS idx_kb_docs_doctype ON knowledgebase_documents(doc_type);

    -- Knowledgebase chunks (with vector embeddings)
    CREATE TABLE IF NOT EXISTS knowledgebase_chunks (
        id SERIAL PRIMARY KEY,
        document_id INTEGER REFERENCES knowledgebase_documents(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        chunk_text TEXT NOT NULL,
        chunk_type TEXT DEFAULT 'regular',
        heading TEXT,
        heading_level INTEGER,
        section_path TEXT,
        keywords TEXT,
        token_count INTEGER,
        start_position INTEGER,
        end_position INTEGER,
        page_number INTEGER,
        parent_id INTEGER,  -- FK added after table creation
        part_index INTEGER,
        total_parts INTEGER,
        embedding vector(1536),
        related_chunk_ids JSONB DEFAULT '[]',
        section_chunk_ids JSONB DEFAULT '[]',
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    -- Add self-referencing foreign key after table exists
    ALTER TABLE knowledgebase_chunks
        ADD CONSTRAINT fk_kb_chunks_parent
        FOREIGN KEY (parent_id) REFERENCES knowledgebase_chunks(id) ON DELETE SET NULL;

    CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON knowledgebase_chunks(document_id);
    CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding ON knowledgebase_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200);

    -- ============================================
    -- MEMORY TABLE (MongoDB replacement for memory agent)
    -- ============================================
    -- user_id is TEXT because it can be an external agent ID, not necessarily a users.id

    CREATE TABLE IF NOT EXISTS memories (
        id SERIAL PRIMARY KEY,
        memory_id TEXT UNIQUE NOT NULL,
        agent_id TEXT NOT NULL,
        user_id TEXT,
        content TEXT NOT NULL,
        type TEXT DEFAULT 'fact',
        importance_score FLOAT DEFAULT 0,
        scores JSONB DEFAULT '{}',
        scope TEXT DEFAULT 'personal',
        tags TEXT[] DEFAULT '{}',
        raw_context TEXT,
        access_count INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_accessed_at TIMESTAMPTZ
    );

    CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
    CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
    CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance_score DESC);
    CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN (tags);

    -- ============================================
    -- GRANT PERMISSIONS
    -- ============================================

    -- Grant all privileges to the application user
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO CURRENT_USER;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO CURRENT_USER;

EOSQL

echo "✅ PostgreSQL initialization complete"
