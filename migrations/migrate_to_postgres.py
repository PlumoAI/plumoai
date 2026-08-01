#!/usr/bin/env python3
"""
PlumoAI Database Migration: MySQL + MongoDB + Milvus → PostgreSQL

This script migrates data from the legacy 3-database stack to the unified
PostgreSQL database with pgvector.

Usage:
    python migrate_to_postgres.py --env production

Prerequisites:
    - PostgreSQL running with pgvector extension
    - MySQL accessible (for auth data)
    - MongoDB accessible (for memories, agent states)
    - Milvus accessible (for vector embeddings)
"""

import os
import sys
import json
import hashlib
import argparse
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

# Third-party imports
try:
    import asyncpg
    import pymongo
    import pymysql
    import httpx
except ImportError as e:
    print(f"Missing required package: {e}")
    print("Install with: pip install asyncpg pymongo pymysql httpx")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class PostgresMigrator:
    """Migrates data from MySQL, MongoDB, and Milvus to PostgreSQL."""

    def __init__(self, config: Dict[str, str]):
        self.config = config
        self.pg_pool = None
        self.mysql_conn = None
        self.mongo_client = None
        self.stats = {
            "auth_users": 0,
            "companies": 0,
            "agents": 0,
            "memories": 0,
            "document_chunks": 0,
            "knowledgebase_docs": 0,
        }

    async def connect(self):
        """Establish connections to all databases."""
        # PostgreSQL
        self.pg_pool = await asyncpg.create_pool(
            host=self.config["PG_HOST"],
            port=int(self.config.get("PG_PORT", 5432)),
            database=self.config["PG_DATABASE"],
            user=self.config["PG_USER"],
            password=self.config["PG_PASSWORD"],
            min_size=2,
            max_size=10
        )
        logger.info("Connected to PostgreSQL")

        # MySQL
        self.mysql_conn = pymysql.connect(
            host=self.config["MYSQL_HOST"],
            port=int(self.config.get("MYSQL_PORT", 3306)),
            user=self.config["MYSQL_USER"],
            password=self.config["MYSQL_PASSWORD"],
            database=self.config.get("MYSQL_DATABASE", "authdb_prod"),
            charset='utf8mb4'
        )
        logger.info("Connected to MySQL")

        # MongoDB
        mongo_uri = self.config.get("MONGODB_URI", "mongodb://localhost:27017")
        self.mongo_client = pymongo.MongoClient(mongo_uri)
        logger.info("Connected to MongoDB")

    async def close(self):
        """Close all connections."""
        if self.pg_pool:
            await self.pg_pool.close()
        if self.mysql_conn:
            self.mysql_conn.close()
        if self.mongo_client:
            self.mongo_client.close()

    # =========================================================================
    # MySQL → PostgreSQL Migration
    # =========================================================================

    async def migrate_auth_users(self):
        """Migrate users from MySQL authdb_prod to PostgreSQL."""
        logger.info("Migrating auth users from MySQL...")

        cursor = self.mysql_conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users")
        users = cursor.fetchall()

        async with self.pg_pool.acquire() as conn:
            for user in users:
                try:
                    await conn.execute("""
                        INSERT INTO auth_users (id, email, password_hash, email_verified, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (email) DO NOTHING
                    """,
                        user.get("id"),
                        user.get("email"),
                        user.get("password_hash"),
                        user.get("email_verified", False),
                        user.get("created_at", datetime.now()),
                        user.get("updated_at", datetime.now())
                    )
                    self.stats["auth_users"] += 1
                except Exception as e:
                    logger.warning(f"Failed to migrate user {user.get('email')}: {e}")

        logger.info(f"Migrated {self.stats['auth_users']} auth users")

    async def migrate_companies(self):
        """Migrate companies from MySQL to PostgreSQL."""
        logger.info("Migrating companies from MySQL...")

        cursor = self.mysql_conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM companies")
        companies = cursor.fetchall()

        async with self.pg_pool.acquire() as conn:
            for company in companies:
                try:
                    settings = json.dumps(company.get("settings") or {})
                    await conn.execute("""
                        INSERT INTO companies (id, name, domain, settings, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (id) DO NOTHING
                    """,
                        company.get("id"),
                        company.get("name"),
                        company.get("domain"),
                        settings,
                        company.get("created_at", datetime.now()),
                        company.get("updated_at", datetime.now())
                    )
                    self.stats["companies"] += 1
                except Exception as e:
                    logger.warning(f"Failed to migrate company {company.get('name')}: {e}")

        logger.info(f"Migrated {self.stats['companies']} companies")

    async def migrate_agents(self):
        """Migrate agents from MySQL to PostgreSQL."""
        logger.info("Migrating agents from MySQL...")

        cursor = self.mysql_conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM agents")
        agents = cursor.fetchall()

        async with self.pg_pool.acquire() as conn:
            for agent in agents:
                try:
                    config = json.dumps(agent.get("config") or {})
                    model_config = json.dumps(agent.get("model_config") or {})
                    await conn.execute("""
                        INSERT INTO agents (id, company_id, name, description, config, model_config, is_active, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (id) DO NOTHING
                    """,
                        agent.get("id"),
                        agent.get("company_id"),
                        agent.get("name"),
                        agent.get("description"),
                        config,
                        model_config,
                        agent.get("is_active", True),
                        agent.get("created_at", datetime.now()),
                        agent.get("updated_at", datetime.now())
                    )
                    self.stats["agents"] += 1
                except Exception as e:
                    logger.warning(f"Failed to migrate agent {agent.get('name')}: {e}")

        logger.info(f"Migrated {self.stats['agents']} agents")

    # =========================================================================
    # MongoDB → PostgreSQL Migration
    # =========================================================================

    async def migrate_memories(self):
        """Migrate memories from MongoDB to PostgreSQL."""
        logger.info("Migrating memories from MongoDB...")

        mdb = self.mongo_client["plumoai_mongo"]
        memories = list(mdb["memories"].find({}))

        async with self.pg_pool.acquire() as conn:
            for mem in memories:
                try:
                    memory_id = str(mem.get("_id", ""))
                    if not memory_id:
                        memory_id = hashlib.md5(json.dumps(mem.get("content", ""), default=str).encode()).hexdigest()[:16]

                    scores = json.dumps(mem.get("scores") or {})
                    tags = json.dumps(mem.get("tags") or [])

                    await conn.execute("""
                        INSERT INTO memories (memory_id, agent_id, user_id, company_id, content, type, scope,
                            importance_score, scores, tags, raw_context, access_count,
                            last_accessed_at, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                        ON CONFLICT (memory_id) DO NOTHING
                    """,
                        memory_id,
                        str(mem.get("agent_id", "")),
                        str(mem.get("user_id", "")),
                        str(mem.get("company_id", "")),
                        mem.get("content", ""),
                        mem.get("type", "fact"),
                        mem.get("scope", "personal"),
                        mem.get("importance_score", 0.0),
                        scores,
                        tags,
                        mem.get("raw_context"),
                        mem.get("access_count", 0),
                        mem.get("last_accessed_at"),
                        mem.get("createdAt", datetime.now()),
                        mem.get("updatedAt", datetime.now())
                    )
                    self.stats["memories"] += 1
                except Exception as e:
                    logger.warning(f"Failed to migrate memory: {e}")

        logger.info(f"Migrated {self.stats['memories']} memories")

    async def migrate_agent_states(self):
        """Migrate agent states from MongoDB to PostgreSQL."""
        logger.info("Migrating agent states from MongoDB...")

        mdb = self.mongo_client["plumoai_mongo"]
        states = list(mdb["agent_states"].find({}))

        async with self.pg_pool.acquire() as conn:
            for state in states:
                try:
                    state_data = json.dumps(state.get("state") or {})
                    await conn.execute("""
                        INSERT INTO agent_states (agent_id, user_id, session_id, state, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                        state.get("agent_id"),
                        state.get("user_id"),
                        state.get("session_id"),
                        state_data,
                        state.get("created_at", datetime.now()),
                        state.get("updated_at", datetime.now())
                    )
                except Exception as e:
                    logger.warning(f"Failed to migrate agent state: {e}")

        logger.info(f"Migrated {len(states)} agent states")

    # =========================================================================
    # Milvus → PostgreSQL Migration
    # =========================================================================

    async def migrate_knowledgebase(self):
        """Migrate knowledgebase documents and chunks from Milvus to PostgreSQL."""
        logger.info("Migrating knowledgebase from Milvus...")

        milvus_url = self.config.get("MILVUS_URL", "http://milvus-standalone:19530")

        async with httpx.AsyncClient(timeout=60.0) as client:
            # Get all collections
            try:
                resp = await client.post(f"{milvus_url}/v2/vectordb/collections/list")
                collections = resp.json().get("data", {}).get("collection_names", [])
            except Exception as e:
                logger.warning(f"Could not connect to Milvus: {e}")
                logger.info("Skipping Milvus migration - will use empty knowledgebase")
                return

            for collection_name in collections:
                try:
                    await self._migrate_milvus_collection(client, collection_name, milvus_url)
                except Exception as e:
                    logger.warning(f"Failed to migrate collection {collection_name}: {e}")

        logger.info(f"Migrated {self.stats['knowledgebase_docs']} knowledgebase docs and {self.stats['document_chunks']} chunks")

    async def _migrate_milvus_collection(self, client, collection_name: str, milvus_url: str):
        """Migrate a single Milvus collection to PostgreSQL."""
        logger.info(f"Migrating Milvus collection: {collection_name}")

        # Query all vectors (with pagination)
        offset = 0
        batch_size = 1000

        while True:
            resp = await client.post(f"{milvus_url}/v2/vectordb/entities/query", json={
                "collection_name": collection_name,
                "filter": "",
                "output_fields": ["*"],
                "limit": batch_size,
                "offset": offset
            })

            data = resp.json().get("data", {})
            entities = data.get("entities", [])

            if not entities:
                break

            async with self.pg_pool.acquire() as conn:
                for entity in entities:
                    try:
                        # Extract fields from Milvus entity
                        doc_id = entity.get("document_id", 0)
                        chunk_index = entity.get("chunk_index", 0)
                        chunk_text = entity.get("chunk_text", "")
                        chunk_type = entity.get("chunk_type", "text")
                        heading = entity.get("heading", "")
                        heading_level = entity.get("heading_level", 0)
                        section_path = entity.get("section_path", "")
                        keywords = entity.get("keywords", "")
                        page_number = entity.get("page_number", 0)
                        doc_type = entity.get("doc_type", "general")
                        language = entity.get("language", "en")
                        project_fid = entity.get("project_fid", "")
                        related_chunk_ids = json.dumps(entity.get("related_chunk_ids") or [])
                        section_chunk_ids = json.dumps(entity.get("section_chunk_ids") or [])
                        embedding = entity.get("vector", [])

                        # Get company_id from document
                        doc_resp = await client.get(
                            f"{milvus_url}/v2/vectordb/collections/get",
                            params={"collection_name": collection_name, "id": doc_id}
                        )
                        doc_data = doc_resp.json().get("data", {})
                        company_id = doc_data.get("company_id", 0)

                        await conn.execute("""
                            INSERT INTO document_chunks (document_id, company_id, chunk_index, chunk_text,
                                chunk_type, heading, heading_level, section_path, keywords, page_number,
                                doc_type, language, project_fid, related_chunk_ids, section_chunk_ids, embedding)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                        """,
                            doc_id,
                            company_id,
                            chunk_index,
                            chunk_text,
                            chunk_type,
                            heading,
                            heading_level,
                            section_path,
                            keywords,
                            page_number,
                            doc_type,
                            language,
                            project_fid,
                            related_chunk_ids,
                            section_chunk_ids,
                            str(embedding) if embedding else None
                        )
                        self.stats["document_chunks"] += 1
                    except Exception as e:
                        logger.warning(f"Failed to migrate chunk: {e}")

            offset += batch_size

            if len(entities) < batch_size:
                break

    # =========================================================================
    # Validation
    # =========================================================================

    async def validate_migration(self):
        """Validate that migration was successful."""
        logger.info("Validating migration...")

        async with self.pg_pool.acquire() as conn:
            # Count records
            auth_count = await conn.fetchval("SELECT COUNT(*) FROM auth_users")
            company_count = await conn.fetchval("SELECT COUNT(*) FROM companies")
            agent_count = await conn.fetchval("SELECT COUNT(*) FROM agents")
            memory_count = await conn.fetchval("SELECT COUNT(*) FROM memories")
            chunk_count = await conn.fetchval("SELECT COUNT(*) FROM document_chunks")

            logger.info(f"PostgreSQL record counts:")
            logger.info(f"  Auth users: {auth_count}")
            logger.info(f"  Companies: {company_count}")
            logger.info(f"  Agents: {agent_count}")
            logger.info(f"  Memories: {memory_count}")
            logger.info(f"  Document chunks: {chunk_count}")

            # Verify pgvector works
            try:
                await conn.execute("SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1")
                logger.info("  pgvector: OK")
            except Exception as e:
                logger.error(f"  pgvector: FAILED - {e}")

            # Verify trigram index works
            try:
                await conn.execute("SELECT * FROM memories WHERE content % 'test' LIMIT 1")
                logger.info("  pg_trgm: OK")
            except Exception as e:
                logger.warning(f"  pg_trgm: Warning - {e}")


async def main():
    parser = argparse.ArgumentParser(description="Migrate PlumoAI databases to PostgreSQL")
    parser.add_argument("--env", default="production", help="Environment (production, staging, development)")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, don't migrate")
    args = parser.parse_args()

    # Load config from environment
    config = {
        "PG_HOST": os.getenv("PG_HOST", "localhost"),
        "PG_PORT": os.getenv("PG_PORT", "5432"),
        "PG_DATABASE": os.getenv("PG_DATABASE", "plumoai"),
        "PG_USER": os.getenv("PG_USER", "plumoai_user"),
        "PG_PASSWORD": os.getenv("PG_PASSWORD", ""),
        "MYSQL_HOST": os.getenv("MYSQL_HOST", "localhost"),
        "MYSQL_PORT": os.getenv("MYSQL_PORT", "3306"),
        "MYSQL_USER": os.getenv("MYSQL_USER", "root"),
        "MYSQL_PASSWORD": os.getenv("MYSQL_PASSWORD", ""),
        "MYSQL_DATABASE": os.getenv("MYSQL_DATABASE", "authdb_prod"),
        "MONGODB_URI": os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
        "MILVUS_URL": os.getenv("MILVUS_URL", "http://milvus-standalone:19530"),
    }

    migrator = PostgresMigrator(config)

    try:
        await migrator.connect()

        if not args.dry_run:
            logger.info("Starting migration...")
            await migrator.migrate_auth_users()
            await migrator.migrate_companies()
            await migrator.migrate_agents()
            await migrator.migrate_memories()
            await migrator.migrate_agent_states()
            await migrator.migrate_knowledgebase()
        else:
            logger.info("Dry run mode - validating connections only")

        await migrator.validate_migration()

        logger.info("Migration completed successfully!")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        await migrator.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
