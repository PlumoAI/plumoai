# PlumoAI Migration Guide: v1.x → v2.x

## Overview

PlumoAI v2.0 consolidates three databases (MySQL + MongoDB + Milvus) into a single PostgreSQL instance with pgvector. This reduces idle RAM from **16 GB+ to ~1-2 GB** and simplifies operations.

## What Changed

### Database Consolidation

| Before (v1.x) | After (v2.0) |
|----------------|--------------|
| MySQL 8.0 | PostgreSQL 16 + pgvector |
| MongoDB 7 | PostgreSQL 16 (JSONB columns) |
| Milvus 2.6.14 + etcd + MinIO | PostgreSQL 16 (pgvector HNSW) |
| **Total: ~5.5 GB RAM** | **Total: ~1-2 GB RAM** |

### Service Changes

- **Auth service**: Now connects to PostgreSQL instead of MySQL
- **Company service**: Now connects to PostgreSQL instead of MySQL + MongoDB + Milvus
- **AI service**: No changes (uses HTTP API to company service)

### Agent Plugins

**No changes required** to agent plugins. They continue to use the same HTTP API endpoints:
- Memory agent: `/aiagentchat/memory/*`
- Knowledgebase agent: `/aiagentchat/knowledgebase/*`

## Migration Steps

### Prerequisites

1. Backup existing data
2. Ensure PostgreSQL with pgvector is running
3. Python 3.9+ with `asyncpg`, `pymysql`, `pymongo` installed

### Step 1: Backup Existing Data

```bash
# Backup MySQL
mysqldump -h mysql -u root -p"$MYSQL_ROOT_PASSWORD" --all-databases > mysql_backup.sql

# Backup MongoDB
mongodump --uri="$MONGODB_URI" --out=mongo_backup/

# Backup Milvus (if using)
# Export via Milvus REST API or use milvus-backup tool
```

### Step 2: Start New Stack

```bash
# Pull new images
docker compose pull

# Start with PostgreSQL
docker compose up -d postgres
```

### Step 3: Run Migration Script

```bash
# Install migration dependencies
pip install asyncpg pymysql pymongo httpx

# Run migration
export PG_HOST=localhost
export PG_DATABASE=plumoai
export PG_USER=plumoai_user
export PG_PASSWORD=<your_password>

export MYSQL_HOST=localhost
export MYSQL_USER=root
export MYSQL_PASSWORD=<your_mysql_password>

export MONGODB_URI=mongodb://localhost:27017

python migrations/migrate_to_postgres.py
```

### Step 4: Start All Services

```bash
docker compose up -d
```

### Step 5: Verify

```bash
# Check service health
docker compose ps

# Check database
docker compose exec postgres psql -U plumoai_user -d plumoai -c "SELECT COUNT(*) FROM memories;"

# Check logs
docker compose logs -f ai
```

## Rollback

If you need to rollback to v1.x:

1. Stop v2.0 services: `docker compose down`
2. Restore MySQL: `mysql < mysql_backup.sql`
3. Restore MongoDB: `mongorestore mongo_backup/`
4. Start v1.x stack: `docker compose -f docker-compose.yml up -d`

## New Deployment

For new installations:

```bash
# Clone repository
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai

# Initialize configuration
python plumo-cli/cli.py init

# Edit .env with your settings
vim .env

# Start services
python plumo-cli/cli.py start

# Check status
python plumo-cli/cli.py status
```

## Troubleshooting

### "relation does not exist" Error

The database schema wasn't initialized. Run:

```bash
docker compose exec postgres psql -U plumoai_user -d plumoai -f /docker-entrypoint-initdb.d/init-postgres.sh
```

### "connection refused" Error

PostgreSQL isn't ready yet. Wait 30 seconds and retry.

### Memory agent not working

Check that the company service can connect to PostgreSQL:

```bash
docker compose logs company | grep -i "database"
```

## Support

For issues, join our Discord: https://discord.gg/WarY2yWZkg
