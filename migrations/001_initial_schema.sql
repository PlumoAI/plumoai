-- =============================================================================
-- PlumoAI PostgreSQL Schema
-- Unified database: relational + JSONB document store + pgvector
-- Replaces: MySQL + MongoDB + Milvus
-- =============================================================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- For text similarity search

-- =============================================================================
-- AUTH SERVICE TABLES (replacing MySQL authdb_prod)
-- =============================================================================

CREATE TABLE IF NOT EXISTS auth_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email_verified BOOLEAN DEFAULT FALSE,
    reset_token VARCHAR(255),
    reset_token_expires TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INTEGER REFERENCES auth_users(id) ON DELETE CASCADE,
    token_hash VARCHAR(255) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_verification_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES auth_users(id) ON DELETE CASCADE,
    token VARCHAR(255) NOT NULL,
    type VARCHAR(50) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- COMPANY SERVICE TABLES (replacing MySQL prod_* tables)
-- =============================================================================

CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    domain VARCHAR(255),
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agents (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    config JSONB DEFAULT '{}',
    model_config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_knowledgebase (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    file_type VARCHAR(50),
    source_path TEXT,
    doc_type VARCHAR(50) DEFAULT 'general',
    language VARCHAR(10) DEFAULT 'en',
    status VARCHAR(50) DEFAULT 'active',
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kb_agent ON agent_knowledgebase(agent_id);
CREATE INDEX IF NOT EXISTS idx_kb_company ON agent_knowledgebase(company_id);

-- =============================================================================
-- DOCUMENT CHUNKS TABLE (replacing Milvus vector collection)
-- =============================================================================

CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id INTEGER REFERENCES agent_knowledgebase(id) ON DELETE CASCADE,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_type VARCHAR(50) DEFAULT 'text',
    heading VARCHAR(500),
    heading_level INTEGER,
    section_path TEXT,
    keywords TEXT,
    page_number INTEGER,
    parent_id INTEGER,  -- FK added after table creation
    part_index INTEGER,
    total_parts INTEGER,
    token_count INTEGER,
    start_position INTEGER,
    end_position INTEGER,
    doc_type VARCHAR(50) DEFAULT 'general',
    language VARCHAR(10) DEFAULT 'en',
    related_chunk_ids JSONB DEFAULT '[]',
    section_chunk_ids JSONB DEFAULT '[]',
    project_fid VARCHAR(100),
    embedding vector(1536),  -- OpenAI ada-002 dimension; adjust if using different model
    created_at TIMESTAMP DEFAULT NOW()
);

-- Add self-referencing foreign key after table exists
ALTER TABLE document_chunks
    ADD CONSTRAINT fk_doc_chunks_parent
    FOREIGN KEY (parent_id) REFERENCES document_chunks(id) ON DELETE SET NULL;

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON document_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Filtering indexes
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_company_id ON document_chunks(company_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_type ON document_chunks(doc_type);
CREATE INDEX IF NOT EXISTS idx_chunks_project_fid ON document_chunks(project_fid);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_index ON document_chunks(document_id, chunk_index);

-- =============================================================================
-- MEMORY TABLES (replacing MongoDB memories collection)
-- =============================================================================

CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    memory_id VARCHAR(100) UNIQUE NOT NULL,
    agent_id VARCHAR(100) NOT NULL,
    user_id VARCHAR(100) NOT NULL,
    company_id VARCHAR(100),
    content TEXT NOT NULL,
    type VARCHAR(100),
    scope VARCHAR(50) DEFAULT 'personal',
    importance_score FLOAT DEFAULT 0.0,
    scores JSONB DEFAULT '{}',
    tags JSONB DEFAULT '[]',
    raw_context TEXT,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_agent_user ON memories(agent_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance_score DESC);
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm ON memories USING gin (content gin_trgm_ops);

-- =============================================================================
-- AGENT STATE & DYNAMIC TOOL OUTPUTS (replacing MongoDB flexible docs)
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_states (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    user_id INTEGER,
    session_id VARCHAR(100),
    state JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_states_agent ON agent_states(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_states_user ON agent_states(user_id);

CREATE TABLE IF NOT EXISTS tool_outputs (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    user_id INTEGER,
    tool_name VARCHAR(100) NOT NULL,
    output_data JSONB NOT NULL DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_outputs_agent ON tool_outputs(agent_id, user_id);
CREATE INDEX IF NOT EXISTS idx_tool_outputs_tool ON tool_outputs(tool_name);

-- =============================================================================
-- SERVICE PROVIDERS (replacing MySQL service_providers table)
-- =============================================================================

CREATE TABLE IF NOT EXISTS service_providers (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    code VARCHAR(100) NOT NULL,
    name VARCHAR(255) NOT NULL,
    auth_type VARCHAR(50),
    required_fields JSONB DEFAULT '[]',
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- FILE STORAGE METADATA (replacing MongoDB file_metadata collection)
-- =============================================================================

CREATE TABLE IF NOT EXISTS file_metadata (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    filename VARCHAR(500) NOT NULL,
    original_name VARCHAR(500),
    mime_type VARCHAR(100),
    size_bytes BIGINT,
    storage_path TEXT,
    storage_backend VARCHAR(50) DEFAULT 'local',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- PROJECT MANAGEMENT TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(50) DEFAULT 'active',
    config JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    assigned_to VARCHAR(100),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    priority VARCHAR(20) DEFAULT 'medium',
    due_date TIMESTAMP,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- =============================================================================
-- EXECUTION LOGS
-- =============================================================================

CREATE TABLE IF NOT EXISTS execution_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(100) NOT NULL,
    user_id INTEGER,
    company_id INTEGER,
    session_id VARCHAR(100),
    operation VARCHAR(100),
    input_data JSONB DEFAULT '{}',
    output_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'success',
    duration_ms INTEGER,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exec_logs_agent ON execution_logs(agent_id);
CREATE INDEX IF NOT EXISTS idx_exec_logs_session ON execution_logs(session_id);

-- =============================================================================
-- VIEWS FOR COMMON QUERIES
-- =============================================================================

CREATE OR REPLACE VIEW v_agent_search_context AS
SELECT
    dc.id as chunk_id,
    dc.document_id,
    dc.chunk_index,
    dc.chunk_text,
    dc.chunk_type,
    dc.heading,
    dc.heading_level,
    dc.section_path,
    dc.keywords,
    dc.page_number,
    dc.doc_type,
    dc.language,
    dc.related_chunk_ids,
    dc.section_chunk_ids,
    akb.title,
    akb.file_type,
    akb.source_path,
    akb.project_fid
FROM document_chunks dc
JOIN agent_knowledgebase akb ON dc.document_id = akb.id;

-- =============================================================================
-- GRANT PERMISSIONS
-- =============================================================================

-- Grant all privileges to the application user
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO CURRENT_USER;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO CURRENT_USER;
