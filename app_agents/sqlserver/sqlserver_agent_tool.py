"""
SQL Server Agentic Tool - FINAL IMPROVED VERSION
Fully autonomous SQL Server agent with robust reasoning, schema awareness,
automatic syntax correction, and reduced truncated query errors
"""
import pyodbc
import logging
from typing import Dict, List, Any, Optional, AsyncGenerator
from datetime import datetime
from dataclasses import dataclass, field
import uuid
import json
import re
import asyncio
import hashlib
import time

from backend.services.app_agents.base_tool_agent import BaseToolAgent

logger = logging.getLogger(__name__)
# Set to INFO to show generated queries
logger.setLevel(logging.INFO)


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    SCHEMA = "schema"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    FINAL = "final"
    ERROR = "error"
    VALIDATION = "validation"
    CREDIT_USAGE = "credit_usage"  # Credit usage for a specific action


def event(event_type: str, content: Any):
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content
    }


@dataclass
class AgentState:
    goal: str = ""
    plan: List[str] = field(default_factory=list)
    confidence: float = 0.0
    iteration: int = 0
    errors: List[str] = field(default_factory=list)
    executed_queries: List[Dict] = field(default_factory=list)
    observations: List[Dict] = field(default_factory=list)
    last_error: Optional[str] = None
    discovered_column_values: Dict[str, List[Any]] = field(default_factory=dict)  # "table.column" -> [values]
    exploration_phase: str = "initial"  # "initial", "exploring", "querying", "refining"
    explored_columns: set = field(default_factory=set)  # Track which columns have been explored

    def should_stop(self) -> bool:
        return (
            self.confidence >= 0.85 or
            len(self.errors) > 5 or
            self.iteration >= 10  # Increased to allow for exploration iterations
        )


class SQLServerSchemaInspector:
    def __init__(self, conn, allowed_tables: Optional[Dict[str, Dict[str, Any]]] = None):
        self.conn = conn
        self.allowed_tables = allowed_tables  # If provided, only inspect these tables
        self.tables: Dict[str, List[str]] = {}
        self.relationships: Dict[str, List[Dict]] = {}  # table -> [relationships]
        self.primary_keys: Dict[str, List[str]] = {}  # table -> [pk_columns]
        self.column_types: Dict[str, Dict[str, str]] = {}  # table -> {column: type}

    def inspect(self):
        try:
            cursor = self.conn.cursor()
            
            # If allowed_tables is provided, only inspect those specific tables
            if self.allowed_tables:
                table_names = list(self.allowed_tables.keys())
                logger.info(f"🎯 OPTIMIZED: Only inspecting {len(table_names)} allowed tables (skipping all other tables)")
                
                # Build parameterized query for allowed tables
                placeholders = ','.join(['?' for _ in table_names])
                
                # Initialize empty structures for allowed tables
                for table_name in table_names:
                    self.tables[table_name] = []
                    self.column_types[table_name] = {}
                
                # OPTIMIZED: Get columns only for allowed tables
                cursor.execute(f"""
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_NAME IN ({placeholders})
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                """, *table_names)
                for table_name, col_name, data_type, is_nullable in cursor.fetchall():
                    if table_name in self.tables:
                        self.tables[table_name].append(col_name)
                        self.column_types[table_name][col_name] = data_type
                
                # OPTIMIZED: Get primary keys only for allowed tables
                cursor.execute(f"""
                    SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                    INNER JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                        AND kcu.TABLE_NAME = tc.TABLE_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                        AND kcu.TABLE_NAME IN ({placeholders})
                    ORDER BY kcu.TABLE_NAME, kcu.ORDINAL_POSITION
                """, *table_names)
                for table_name, col_name in cursor.fetchall():
                    if table_name not in self.primary_keys:
                        self.primary_keys[table_name] = []
                    self.primary_keys[table_name].append(col_name)
                
                # Get foreign key relationships only for allowed tables
                cursor.execute(f"""
                    SELECT 
                        fk.TABLE_NAME AS FK_TABLE,
                        fk.COLUMN_NAME AS FK_COLUMN,
                        pk.TABLE_NAME AS PK_TABLE,
                        pk.COLUMN_NAME AS PK_COLUMN,
                        fk.CONSTRAINT_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk
                    INNER JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                        ON fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                    INNER JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk
                        ON rc.UNIQUE_CONSTRAINT_NAME = pk.CONSTRAINT_NAME
                        AND fk.ORDINAL_POSITION = pk.ORDINAL_POSITION
                    WHERE fk.TABLE_NAME IN ({placeholders})
                    ORDER BY fk.TABLE_NAME, fk.ORDINAL_POSITION
                """, *table_names)
                for fk_table, fk_column, pk_table, pk_column, constraint_name in cursor.fetchall():
                    if fk_table not in self.relationships:
                        self.relationships[fk_table] = []
                    self.relationships[fk_table].append({
                        "from_table": fk_table,
                        "from_column": fk_column,
                        "to_table": pk_table,
                        "to_column": pk_column,
                        "constraint": constraint_name
                    })
            else:
                # Original behavior: inspect ALL tables in database
                logger.info(f"📊 Inspecting ALL tables in database (no allowed_tables restriction)")
                
                # Get tables
                cursor.execute("""
                    SELECT TABLE_NAME 
                    FROM INFORMATION_SCHEMA.TABLES 
                    WHERE TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_NAME
                """)
                table_names = [row[0] for row in cursor.fetchall()]
                
                # Initialize empty structures for all tables
                for table_name in table_names:
                    self.tables[table_name] = []
                    self.column_types[table_name] = {}
                
                # OPTIMIZED: Get all columns for all tables in a single query
                cursor.execute("""
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_NAME IN (
                        SELECT TABLE_NAME 
                        FROM INFORMATION_SCHEMA.TABLES 
                        WHERE TABLE_TYPE = 'BASE TABLE'
                    )
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                """)
                for table_name, col_name, data_type, is_nullable in cursor.fetchall():
                    if table_name in self.tables:
                        self.tables[table_name].append(col_name)
                        self.column_types[table_name][col_name] = data_type
                
                # OPTIMIZED: Get all primary keys in a single query
                cursor.execute("""
                    SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                    INNER JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                        AND kcu.TABLE_NAME = tc.TABLE_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                    ORDER BY kcu.TABLE_NAME, kcu.ORDINAL_POSITION
                """)
                for table_name, col_name in cursor.fetchall():
                    if table_name not in self.primary_keys:
                        self.primary_keys[table_name] = []
                    self.primary_keys[table_name].append(col_name)
                
                # Get foreign key relationships (already optimized - single query)
                cursor.execute("""
                    SELECT 
                        fk.TABLE_NAME AS FK_TABLE,
                        fk.COLUMN_NAME AS FK_COLUMN,
                        pk.TABLE_NAME AS PK_TABLE,
                        pk.COLUMN_NAME AS PK_COLUMN,
                        fk.CONSTRAINT_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk
                    INNER JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                        ON fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                    INNER JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk
                        ON rc.UNIQUE_CONSTRAINT_NAME = pk.CONSTRAINT_NAME
                        AND fk.ORDINAL_POSITION = pk.ORDINAL_POSITION
                    ORDER BY fk.TABLE_NAME, fk.ORDINAL_POSITION
                """)
                for fk_table, fk_column, pk_table, pk_column, constraint_name in cursor.fetchall():
                    if fk_table not in self.relationships:
                        self.relationships[fk_table] = []
                    self.relationships[fk_table].append({
                        "from_table": fk_table,
                        "from_column": fk_column,
                        "to_table": pk_table,
                        "to_column": pk_column,
                        "constraint": constraint_name
                    })
            
            cursor.close()
            logger.info(f"✅ Schema inspection complete: {len(self.tables)} tables")
        except Exception as e:
            logger.error(f"Schema inspection error: {e}")
            self.tables = {}
            self.relationships = {}
            self.primary_keys = {}
            self.column_types = {}


class SQLGuard:
    @staticmethod
    def validate(sql: str, permissions: str = "read-only") -> bool:
        sql_upper = sql.upper().strip()
        if permissions == "read-only":
            # Check for write keywords as standalone SQL statements, not in table/column names
            write_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"]
            # Check for CREATE as a statement keyword (not in table names like sys.tables)
            # CREATE should be followed by TABLE, VIEW, etc. to be a write operation
            if re.search(r'\bCREATE\s+(TABLE|VIEW|PROCEDURE|FUNCTION|INDEX|DATABASE)\b', sql_upper):
                raise ValueError("Write operations not allowed in read-only mode: CREATE")
            for keyword in write_keywords:
                # Use word boundary to match keywords, not substrings in identifiers
                if re.search(r'\b' + re.escape(keyword) + r'\b', sql_upper):
                    raise ValueError(f"Write operations not allowed in read-only mode: {keyword}")
        if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
            raise ValueError("Only SELECT and WITH queries are supported")
        return True


class SQLServerToolExecutor:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn: Optional[pyodbc.Connection] = None

    def connect(self):
        try:
            self.conn = pyodbc.connect(self.connection_string, timeout=10)
            logger.debug("Connected to SQL Server")
        except pyodbc.InterfaceError as e:
            if "Login failed" in str(e) or "18456" in str(e):
                raise ConnectionError("SQL Server authentication failed.")
            raise
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {str(e)}")

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
                self.conn = None
                logger.debug("Disconnected from SQL Server")
            except Exception as e:
                logger.error(f"Disconnect error: {e}")

    def _check_connection(self) -> bool:
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except:
            return False

    def execute_query(self, sql: str) -> Dict[str, Any]:
        if not self.conn:
            raise ConnectionError("Connection not established")
        if not self._check_connection():
            try:
                self.connect()
            except Exception as e:
                return {"success": False, "error": f"Connection lost: {e}", "row_count": 0, "data": None}

        cursor = None
        try:
            cursor = self.conn.cursor()
            sql_upper = sql.upper().strip()
            if sql_upper.startswith("SELECT") or sql_upper.startswith("WITH"):
                try:
                    cursor.execute(sql)
                    rows = cursor.fetchall()
                    columns = [col[0] for col in cursor.description] if cursor.description else []
                    result = []
                    for row in rows:
                        row_dict = {}
                        for i, col in enumerate(columns):
                            value = row[i]
                            row_dict[col] = value.isoformat() if isinstance(value, datetime) else str(value) if value is not None else None
                        result.append(row_dict)
                    cursor.close()
                    return {"success": True, "row_count": len(result), "data": result, "columns": columns}
                except Exception as fetch_error:
                    error_str = str(fetch_error)
                    error_tuple = fetch_error.args if hasattr(fetch_error, 'args') else ()
                    # Handle ODBC type -155 (datetimeoffset) error
                    # Check both string representation and tuple args
                    is_odbc_155_error = (
                        ("ODBC SQL type" in error_str and ("-155" in error_str or "not yet supported" in error_str)) or
                        (error_tuple and len(error_tuple) > 0 and "-155" in str(error_tuple[0]))
                    )
                    
                    if is_odbc_155_error:
                        logger.warning(f"Unsupported ODBC type -155 detected: {error_str}. Converting problematic columns to string...")
                        
                        # CRITICAL: Safely close the original cursor and mark it as None
                        if cursor:
                            try:
                                cursor.close()
                            except Exception:
                                # Cursor might already be closed - that's okay
                                pass
                            cursor = None  # Mark as closed to prevent double-close
                        
                        # Now try the conversion with a fresh cursor
                        convert_cursor = None
                        try:
                            # Get column names from a fresh query
                            test_cursor = self.conn.cursor()
                            test_cursor.execute(sql)
                            columns = [col[0] for col in test_cursor.description] if test_cursor.description else []
                            test_cursor.close()
                            
                            if columns:
                                # Build query with CAST for all columns to NVARCHAR(MAX)
                                column_list = ", ".join([f"CAST([{col}] AS NVARCHAR(MAX)) AS [{col}]" for col in columns])
                                # Remove ORDER BY from inner query if present (for subquery compatibility)
                                sql_for_conversion = sql
                                import re
                                sql_for_conversion = re.sub(
                                    r'\s+ORDER\s+BY\s+[^\s]+(?:\s+[^\s]+)*(?:\s+ASC|\s+DESC)?(?=\s*$|\s*;|\s*\))',
                                    '',
                                    sql_for_conversion,
                                    flags=re.IGNORECASE | re.DOTALL
                                ).strip().rstrip(';')
                                converted_sql = f"SELECT {column_list} FROM ({sql_for_conversion}) AS subquery"
                                logger.info(f"🔄 Converting query to handle ODBC type -155 (removed ORDER BY from subquery)")
                                
                                convert_cursor = self.conn.cursor()
                                convert_cursor.execute(converted_sql)
                                rows = convert_cursor.fetchall()
                                actual_columns = [col[0] for col in convert_cursor.description] if convert_cursor.description else columns
                                
                                result = []
                                for row in rows:
                                    row_dict = {}
                                    for i, col in enumerate(actual_columns):
                                        value = row[i]
                                        row_dict[col] = str(value) if value is not None else None
                                    result.append(row_dict)
                                
                                # Close conversion cursor
                                if convert_cursor:
                                    try:
                                        convert_cursor.close()
                                    except Exception:
                                        pass
                                
                                # Return success - cursor is already None, so finally block won't try to close it
                                return {"success": True, "row_count": len(result), "data": result, "columns": actual_columns}
                        except Exception as convert_error:
                            logger.error(f"Type conversion failed: {convert_error}")
                            # Close conversion cursor if it was created
                            if convert_cursor:
                                try:
                                    convert_cursor.close()
                                except Exception:
                                    pass
                            # Fall through to return error
                    
                    # If we get here, the error wasn't handled or conversion failed
                    # Safely close cursor and mark as None
                    if cursor:
                        try:
                            cursor.close()
                        except Exception:
                            pass
                        cursor = None  # Mark as closed
                    return {"success": False, "error": str(fetch_error), "row_count": 0, "data": None}
            else:
                self.conn.commit()
                rowcount = cursor.rowcount if cursor else 0
                if cursor:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                return {"success": True, "row_count": rowcount, "data": None, "message": "Executed successfully"}
        except Exception as e:
            # Safely close cursor if it exists and is still valid
            if cursor:
                try:
                    if hasattr(cursor, 'connection') and cursor.connection:
                        cursor.close()
                except:
                    pass  # Cursor might already be closed or invalid
            return {"success": False, "error": str(e), "row_count": 0, "data": None}


class SQLServerAgentTool(BaseToolAgent):
    """
    SQL Server Agent Tool
    
    Autonomous SQL Server database agent for executing queries and retrieving data
    """
    
    # Tool responsibility and description (used for tool selection)
    TOOL_NAME = "SQL Server"
    TOOL_DESCRIPTION = """The SQL Server Tool provides autonomous database query capabilities for retrieving, analyzing, and reporting data from SQL Server databases.

RESPONSIBILITIES:
- Execute SQL queries to retrieve data from SQL Server databases
- Discover database schema (tables, columns, relationships)
- Explore enum-like columns (status, type, code, entity) to understand data values
- Generate optimized SQL queries based on natural language requests
- Handle complex queries with JOINs, aggregations, and filters
- Validate and correct SQL syntax automatically
- Provide detailed query reasoning and explanations

USE THIS TOOL WHEN:
- User asks for data from a database
- User requests information that requires database queries
- User wants to retrieve, count, filter, or analyze database records
- User asks "how many", "list all", "show me", "get data", "query database", etc.
- Data is not available in conversation history

DO NOT USE THIS TOOL WHEN:
- Information is already in conversation history
- User asks simple questions about previous responses
- User is asking about something just mentioned in the conversation
- No database access is needed"""
    
    def __init__(
        self, 
        connection_string: str, 
        llm_provider, 
        permissions: str = "read-only", 
        agent_instructions: Optional[str] = None,
        db_instructions: Optional[str] = None,
        allowed_tables: Optional[Dict[str, Dict[str, Any]]] = None,
        specific_filter_instructions: Optional[str] = None,
        app_id: Optional[int] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        app_code: Optional[str] = None,
        agent_id: Optional[str] = None
    ):
        """
        Initialize SQL Server Agent Tool
        
        Args:
            connection_string: SQL Server connection string
            llm_provider: LLM provider instance for reasoning
            permissions: "read-only" or "read-write"
            agent_instructions: Optional instructions for the SQL agent
            db_instructions: Optional database-level instructions (e.g., "Use specific naming conventions")
            allowed_tables: Optional dict of allowed tables with their allowed columns and descriptions
                          Format: {
                              "TableName": {
                                  "allowed_columns": ["Column1", "Column2", ...],
                                  "description": "Table description"
                              },
                              ...
                          }
                          If provided, only these tables and columns will be accessible
            specific_filter_instructions: Optional specific filtering instructions (e.g., "Always filter by TenantID = 1")
            app_id: Optional app ID for credit tracking
            token: Optional authentication token for credit tracking
            company_id: Optional company ID for credit tracking
            user_id: Optional user ID for credit tracking
            app_code: Optional app code (e.g., "sqlserver") for credit tracking when app_id is not available
            agent_id: Optional agent ID to include in credit ledger reference_id
        """
        self.connection_string = connection_string
        self.llm_provider = llm_provider
        self.permissions = permissions
        self.agent_instructions = agent_instructions or "Generate accurate T-SQL queries"
        self.db_instructions = db_instructions  # Database-level instructions
        self.allowed_tables = allowed_tables or {}  # Dict: table_name -> {allowed_columns: [...], description: "..."}
        self.specific_filter_instructions = specific_filter_instructions  # Specific filtering instructions
        self.executor = SQLServerToolExecutor(connection_string)
        self.schema_inspector = None
        self.state = AgentState()
        self._initialized = False
        self.query_cache: Dict[str, Dict[str, Any]] = {}
        self.executed_sql_hashes: set = set()  # Track executed SQL to prevent infinite retries
        self.llm_response_cache: Dict[str, str] = {}  # Cache LLM responses to reduce API calls
        self._last_llm_call_time: float = 0.0  # Track last LLM call time for rate limiting
        self._llm_call_delay: float = 0.5  # Minimum delay between LLM calls (500ms)
        
        # Credit tracking
        self.app_id = app_id
        self.app_code = app_code
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.credit_usage_list = []  # Track credit usage for each action

    @classmethod
    def get_tool_responsibility(cls) -> str:
        """
        Get tool responsibility description (to be appended to custom_description)
        
        Returns:
            Tool responsibility description string
        """
        return cls.TOOL_DESCRIPTION
    
    async def initialize(self):
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.executor.connect)
            # Pass allowed_tables to schema inspector for optimized inspection
            self.schema_inspector = SQLServerSchemaInspector(self.executor.conn, self.allowed_tables)
            await loop.run_in_executor(None, self.schema_inspector.inspect)
            self._initialized = True
            logger.debug("SQL Server Agent initialized")
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            raise

    async def cleanup(self):
        try:
            if self.executor:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.executor.disconnect)
            self._initialized = False
            logger.debug("SQL Server Agent cleaned up")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    def _extract_sql_from_text(self, text: str) -> Optional[str]:
        """
        Enhanced SQL extraction - handles multiple formats like MySQL agent
        """
        if not text:
            return None
        
        # First: Look for "SQL:" marker (from constrained prompt)
        if "SQL:" in text:
            sql_part = text.split("SQL:", 1)[1].strip()
            # Extract ALL lines until we hit an explanation marker or end
            # Be more lenient - only stop at clear explanation markers, not empty lines
            lines = []
            for line in sql_part.split("\n"):
                stripped = line.strip()
                # Stop at clear explanation markers (but allow empty lines within SQL)
                if stripped and stripped.lower().startswith(("here", "this", "note:", "explanation", "the query", "this query", "```", "---", "response:", "answer:")):
                    break
                # Stop if we hit another SQL marker
                if "SQL:" in stripped and len(lines) > 0:  # Only stop if we already have SQL
                    break
                # Include the line (even if empty, to preserve SQL formatting)
                lines.append(line)
            sql = "\n".join(lines).strip()
            # Remove any remaining markers or explanations
            if sql.startswith("```"):
                sql = sql.replace("```", "").strip()
            # Remove trailing semicolons
            sql = re.sub(r';\s*$', '', sql)
            if sql and sql.upper().startswith(("SELECT", "WITH")):
                return sql
        
        # Second: Look for SQL in code blocks (```sql ... ``` or ``` ... ```)
        code_block_pattern = r'```(?:sql)?\s*(SELECT.*?)(?:;|\s*```)'
        code_match = re.search(code_block_pattern, text, re.IGNORECASE | re.DOTALL)
        if code_match:
            sql = code_match.group(1).strip()
            if sql and sql.upper().startswith(("SELECT", "WITH")):
                return sql
        
        # Third: Look for standalone SQL statements
        sql_pattern = r'(SELECT\s+(?:(?!SELECT|FROM|WHERE|JOIN|GROUP|ORDER|LIMIT|TOP)[\s\S])*?FROM\s+[\s\S]*?(?:WHERE|JOIN|GROUP|ORDER|TOP|;|$))'
        sql_match = re.search(sql_pattern, text, re.IGNORECASE | re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
            # Clean up
            sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)  # Remove comments
            sql = re.sub(r';\s*$', '', sql)  # Remove trailing semicolons
            sql = sql.strip()
            if sql and sql.upper().startswith(("SELECT", "WITH")):
                return sql
        
        # Fallback: Try to extract from plain text
        sql = text.strip()
        # Remove code fences
        sql = re.sub(r'```.*?```', '', sql, flags=re.DOTALL)
        # Remove inline comments
        sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)
        # Remove trailing semicolons
        sql = re.sub(r';\s*$', '', sql)
        sql = sql.strip()
        if not sql.upper().startswith(("SELECT", "WITH")):
            return None
        return sql if len(sql) > 5 else None

    def _normalize_sql(self, sql: str) -> str:
        """
        Normalize SQL for semantic hashing - removes whitespace differences,
        normalizes operators, lowercases keywords
        """
        if not sql:
            return ""
        # Remove extra whitespace
        sql = re.sub(r'\s+', ' ', sql.strip())
        # Normalize operators (spaces around =, <, >, etc.)
        sql = re.sub(r'\s*=\s*', ' = ', sql)
        sql = re.sub(r'\s*<\s*', ' < ', sql)
        sql = re.sub(r'\s*>\s*', ' > ', sql)
        sql = re.sub(r'\s*<=\s*', ' <= ', sql)
        sql = re.sub(r'\s*>=\s*', ' >= ', sql)
        sql = re.sub(r'\s*!=\s*', ' != ', sql)
        sql = re.sub(r'\s*<>\s*', ' <> ', sql)
        # Lowercase SQL keywords (but preserve table/column names case)
        # This is tricky - we'll lowercase common keywords only
        keywords = ['select', 'from', 'where', 'join', 'inner', 'left', 'right', 'outer',
                   'on', 'and', 'or', 'not', 'group', 'order', 'by', 'having', 'top',
                   'distinct', 'as', 'case', 'when', 'then', 'else', 'end', 'union', 'all']
        words = sql.split()
        normalized = []
        for word in words:
            if word.upper() in [k.upper() for k in keywords]:
                normalized.append(word.lower())
            else:
                normalized.append(word)
        return ' '.join(normalized)

    def _fix_common_sql_issues(self, sql: str) -> Optional[str]:
        if not sql:
            return None
        
        # Check for incomplete SQL before fixing
        sql_upper = sql.upper()
        
        # Reject if ends with incomplete keywords (but allow multi-line SQL)
        # Only check if SQL appears to be truncated (single line ending with incomplete keyword)
        incomplete_endings = ["AND", "OR", "WHERE", "JOIN", "UNION", "UNION ALL", "SELECT", "FROM", "ON", "GROUP", "ORDER", "HAVING"]
        # Check last word only if it's a single-line SQL (likely truncated)
        if "\n" not in sql:
            last_word = sql_upper.strip().split()[-1] if sql_upper.strip().split() else ""
            if last_word in incomplete_endings:
                logger.warning(f"Incomplete SQL detected - single line ending with '{last_word}' - rejecting")
                return None
        
        # Remove dangling AND/OR at end (but only if SQL is otherwise complete)
        sql = re.sub(r'\b(AND|OR)\s*$', '', sql, flags=re.IGNORECASE)
        
        # Simple bracket fix for SQL Server
        sql = re.sub(r'\"([^\"]+)\"', r'[\1]', sql)
        
        # Final check: ensure SQL is complete
        if sql_upper.startswith("SELECT") and "FROM" not in sql_upper:
            return None
        
        return sql.strip()

    def _inject_safety_limit(self, sql: str, max_rows: int = 1000) -> str:
        """
        CRITICAL SAFETY: Automatically inject TOP limit into queries without one
        to prevent accidentally fetching millions of rows.
        
        Args:
            sql: The SQL query to check and modify
            max_rows: Maximum rows to return (default: 1000)
            
        Returns:
            SQL with TOP limit injected if needed
        """
        sql_upper = sql.upper().strip()
        
        # Skip if query already has TOP, OFFSET/FETCH, or is an aggregate query
        if re.search(r'\bTOP\s+\d+', sql_upper):
            return sql  # Already has TOP limit
        
        if re.search(r'\bOFFSET\s+\d+\s+ROWS', sql_upper):
            return sql  # Already has OFFSET/FETCH pagination
        
        # Check if it's an aggregate query (COUNT, SUM, AVG, MIN, MAX, GROUP BY)
        # These queries return few rows even without TOP
        if re.search(r'\b(COUNT|SUM|AVG|MIN|MAX)\s*\(', sql_upper):
            return sql  # Aggregate query - safe
        
        if re.search(r'\bGROUP\s+BY\b', sql_upper):
            # GROUP BY might return many rows, but often reasonable
            # Still inject TOP but with higher limit
            max_rows = 5000
        
        # Inject TOP limit into SELECT statement
        # Handle: SELECT, SELECT DISTINCT
        if sql_upper.startswith('SELECT DISTINCT'):
            # SELECT DISTINCT ... -> SELECT DISTINCT TOP N ...
            sql = re.sub(r'^(SELECT\s+DISTINCT)\s+', rf'\1 TOP {max_rows} ', sql, count=1, flags=re.IGNORECASE)
            logger.warning(f"⚠️ SAFETY: Injected TOP {max_rows} into SELECT DISTINCT query to prevent overload")
        elif sql_upper.startswith('SELECT'):
            # SELECT ... -> SELECT TOP N ...
            sql = re.sub(r'^(SELECT)\s+', rf'\1 TOP {max_rows} ', sql, count=1, flags=re.IGNORECASE)
            logger.warning(f"⚠️ SAFETY: Injected TOP {max_rows} into SELECT query to prevent overload")
        
        return sql

    def _sql_server_sanity_check(self, sql: str) -> None:
        sql_upper = sql.upper()
        if re.search(r'\bLIMIT\s+\d+', sql_upper):
            raise ValueError("LIMIT detected. Use TOP/OFFSET for SQL Server")
        if '`' in sql:
            raise ValueError("Backticks detected. Use [] for identifiers")
        if re.search(r'\bILIKE\b', sql_upper):
            raise ValueError("ILIKE detected. Use LIKE")
        if '::' in sql:
            raise ValueError("PostgreSQL type cast (::) detected")
        if re.search(r'\bBOOLEAN\b', sql_upper):
            raise ValueError("BOOLEAN detected. Use BIT")
        if sql_upper.startswith("SELECT") and "FROM" not in sql_upper:
            raise ValueError("SELECT missing FROM clause")

    def _sql_server_parse_check(self, sql: str) -> None:
        if not self.executor or not self.executor.conn:
            return
        cursor = None
        try:
            cursor = self.executor.conn.cursor()
            # First: PARSEONLY check (syntax)
            cursor.execute("SET PARSEONLY ON")
            try:
                cursor.execute(sql)
            except Exception as parse_error:
                raise ValueError(f"SQL parse error: {parse_error}")
            finally:
                try:
                    cursor.execute("SET PARSEONLY OFF")
                except:
                    pass
            
            # Second: TOP 1 dry-run check (semantic validation)
            # This catches missing tables, ambiguous columns, bad GROUP BY, etc.
            # Note: Remove ORDER BY from inner query since SQL Server doesn't allow it in derived tables
            try:
                # Remove ORDER BY clause for validation (it doesn't affect semantic correctness)
                sql_for_validation = sql
                # Find and remove ORDER BY clause (handles multi-line, case-insensitive)
                # Pattern: ORDER BY ... (everything until end of query or closing paren)
                order_by_pattern = r'\s+ORDER\s+BY\s+[^\s]+(?:\s+[^\s]+)*(?:\s+ASC|\s+DESC)?(?=\s*$|\s*;|\s*\))'
                sql_for_validation = re.sub(order_by_pattern, '', sql_for_validation, flags=re.IGNORECASE | re.DOTALL)
                sql_for_validation = sql_for_validation.strip()
                # Remove trailing semicolon if present
                sql_for_validation = sql_for_validation.rstrip(';').strip()
                
                # Wrap query in TOP 1 subquery for dry-run
                dry_run_sql = f"SELECT TOP 1 * FROM ({sql_for_validation}) AS q"
                logger.info(f"🔍 Dry-run SQL for validation (ORDER BY removed):\n{dry_run_sql}\n")
                cursor.execute("SET FMTONLY OFF")
                cursor.execute(dry_run_sql)
                # If it executes without error, semantic validation passes
            except Exception as semantic_error:
                error_str = str(semantic_error).lower()
                # Ignore "no rows" errors - that's fine for dry-run
                # Also ignore ORDER BY errors in dry-run since we're testing semantic correctness
                if "no rows" not in error_str and "invalid object" not in error_str and "order by" not in error_str:
                    logger.error(f"❌ SQL Semantic Error: {semantic_error}\nOriginal Query:\n{sql}\nDry-run Query:\n{dry_run_sql}\n")
                    raise ValueError(f"SQL semantic error: {semantic_error}")
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass

    def _is_valid_sql(self, sql: str) -> bool:
        if not sql or len(sql) < 10:
            return False
        if not sql.upper().strip().startswith(("SELECT", "WITH")):
            return False
        if sql.upper().startswith("SELECT") and "FROM" not in sql.upper():
            return False
        return True

    def _identify_ambiguous_columns(self, user_query: str, tables: Dict[str, List[str]], proactive: bool = False) -> List[tuple]:
        """
        Identify columns that might need value exploration (status, type, entity, code, etc.)
        Returns list of (table_name, column_name) tuples
        
        Args:
            user_query: User's query (for context)
            tables: Dictionary of table_name -> [column_names]
            proactive: If True, scan all tables for enum-like columns regardless of user query
        """
        # Comprehensive list of enum-like keywords
        enum_keywords = [
            'status', 'state', 'type', 'category', 'kind', 'entity', 
            'role', 'level', 'priority', 'stage', 'phase', 'mode',
            'class', 'group', 'classification', 'tag', 'label',
            'code', 'type_code', 'status_code', 'entity_type', 'category_code'
        ]
        
        # Patterns that indicate enum-like columns
        enum_patterns = [
            '_status', '_type', '_state', '_category', '_entity',
            '_code', '_type_code', '_status_code', '_entity_type',
            '_role', '_level', '_priority', '_stage', '_phase',
            '_mode', '_class', '_group', '_classification', '_tag', '_label'
        ]
        
        query_lower = user_query.lower() if user_query else ""
        ambiguous_columns = []
        found_columns = set()  # Track to avoid duplicates
        
        # PROACTIVE MODE: Scan all tables for enum-like columns
        if proactive:
            for table_name, columns in tables.items():
                for column in columns:
                    column_lower = column.lower()
                    col_key = f"{table_name}.{column}"
                    
                    # Skip if already explored
                    if col_key in self.state.explored_columns:
                        continue
                    
                    # Check if column name matches enum keywords
                    if any(keyword in column_lower for keyword in enum_keywords):
                        if col_key not in found_columns:
                            ambiguous_columns.append((table_name, column))
                            found_columns.add(col_key)
                    
                    # Check for enum-like patterns
                    elif any(pattern in column_lower for pattern in enum_patterns):
                        if col_key not in found_columns:
                            ambiguous_columns.append((table_name, column))
                            found_columns.add(col_key)
        
        # QUERY-BASED MODE: Find columns matching user query terms
        else:
            for keyword in enum_keywords:
                if keyword in query_lower:
                    # Find columns matching this keyword across all tables
                    for table_name, columns in tables.items():
                        for column in columns:
                            column_lower = column.lower()
                            col_key = f"{table_name}.{column}"
                            
                            # Skip if already explored
                            if col_key in self.state.explored_columns:
                                continue
                            
                            # Check if column name contains the keyword
                            if keyword in column_lower or column_lower == keyword:
                                if col_key not in found_columns:
                                    ambiguous_columns.append((table_name, column))
                                    found_columns.add(col_key)
            
            # Also check for common ambiguous column patterns
            for table_name, columns in tables.items():
                for column in columns:
                    column_lower = column.lower()
                    col_key = f"{table_name}.{column}"
                    
                    # Skip if already explored
                    if col_key in self.state.explored_columns:
                        continue
                    
                    # Check for common ambiguous patterns
                    if any(pattern in column_lower for pattern in enum_patterns):
                        if col_key not in found_columns:
                            ambiguous_columns.append((table_name, column))
                            found_columns.add(col_key)
        
        return ambiguous_columns

    def _identify_columns_in_query(self, sql: str, tables: Dict[str, List[str]]) -> List[tuple]:
        """
        Identify enum-like columns that are referenced in a SQL query but haven't been explored yet
        This helps proactively explore columns before they're used in filters
        """
        if not sql:
            return []
        
        sql_upper = sql.upper()
        enum_keywords = ['STATUS', 'STATE', 'TYPE', 'CATEGORY', 'ENTITY', 'CODE', 'ROLE', 'LEVEL', 'PRIORITY']
        enum_patterns = ['_STATUS', '_TYPE', '_CODE', '_ENTITY', '_CATEGORY']
        
        columns_to_explore = []
        found_columns = set()
        
        # Extract table names from SQL
        table_names_in_query = []
        for table_name in tables.keys():
            if table_name.upper() in sql_upper or f"[{table_name}]" in sql_upper:
                table_names_in_query.append(table_name)
        
        # For each table in the query, check for enum-like columns
        for table_name in table_names_in_query:
            if table_name not in tables:
                continue
            
            for column in tables[table_name]:
                column_upper = column.upper()
                col_key = f"{table_name}.{column}"
                
                # Skip if already explored
                if col_key in self.state.explored_columns:
                    continue
                
                # Check if column is referenced in SQL
                if column_upper in sql_upper or f"[{column}]" in sql_upper:
                    # Check if it's an enum-like column
                    if any(keyword in column_upper for keyword in enum_keywords):
                        if col_key not in found_columns:
                            columns_to_explore.append((table_name, column))
                            found_columns.add(col_key)
                    elif any(pattern in column_upper for pattern in enum_patterns):
                        if col_key not in found_columns:
                            columns_to_explore.append((table_name, column))
                            found_columns.add(col_key)
        
        return columns_to_explore

    async def _explore_column_values(self, table_name: str, column_name: str) -> Optional[Dict[str, Any]]:
        """
        Query distinct values for a column to understand what types of values exist
        Returns result dict with distinct values
        """
        try:
            # Build query to get distinct values (limit to 50 for performance)
            sql = f"SELECT DISTINCT TOP 50 [{column_name}] FROM [{table_name}] WHERE [{column_name}] IS NOT NULL ORDER BY [{column_name}]"
            
            logger.info(f"🔍 Exploring column values: {table_name}.{column_name}")
            
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.executor.execute_query, sql)
            
            if result.get("success") and result.get("data"):
                values = [row.get(column_name) for row in result.get("data", []) if row.get(column_name) is not None]
                col_key = f"{table_name}.{column_name}"
                self.state.discovered_column_values[col_key] = values
                self.state.explored_columns.add(col_key)
                logger.info(f"✅ Discovered {len(values)} distinct values for {table_name}.{column_name}: {values[:10]}...")
                return result
            else:
                logger.warning(f"⚠️ Could not explore values for {table_name}.{column_name}: {result.get('error', 'Unknown error')}")
                return result
        except Exception as e:
            logger.error(f"❌ Error exploring column {table_name}.{column_name}: {e}")
            return {"success": False, "error": str(e), "row_count": 0, "data": None}

    async def _review_and_validate_sql(self, sql: str, user_query: str) -> Dict[str, Any]:
        """
        SQL Review Agent (inspired by Veeam multi-agent architecture)
        Validates SQL before execution and provides structured feedback for improvement.
        This acts as a separate review step before execution, similar to the article's approach.
        
        Returns: {"valid": bool, "sql": str, "issues": List[str], "suggestions": List[str]}
        """
        issues = []
        suggestions = []
        reviewed_sql = sql
        
        # Print SQL being reviewed for debugging
        logger.info(f"🔍 Reviewing SQL Query:\n{sql}\n")
        
        # Check for common issues
        if not sql or not sql.strip():
            return {"valid": False, "sql": sql, "issues": ["Empty SQL query"], "suggestions": ["Generate a valid SQL query"]}
        
        # CRITICAL SAFETY: Inject TOP limit if query doesn't have one
        # This prevents accidentally fetching millions of rows
        reviewed_sql = self._inject_safety_limit(sql, max_rows=1000)
        
        # Check for SQL Server syntax issues
        try:
            self._sql_server_sanity_check(reviewed_sql)
        except ValueError as e:
            issues.append(f"SQL Server syntax issue: {str(e)}")
            suggestions.append("Fix SQL Server-specific syntax (use TOP instead of LIMIT, [] instead of backticks)")
            logger.warning(f"⚠️ SQL Server syntax issue: {str(e)}")
        
        # Check for read-only violations
        try:
            SQLGuard.validate(reviewed_sql, self.permissions)
        except ValueError as e:
            issues.append(f"Permission violation: {str(e)}")
            suggestions.append("Ensure query is read-only (SELECT/WITH only)")
            logger.warning(f"⚠️ Permission violation: {str(e)}")
        
        # Check for parse errors
        try:
            self._sql_server_parse_check(reviewed_sql)
        except ValueError as e:
            issues.append(f"Parse error: {str(e)}")
            suggestions.append("Fix SQL syntax errors before execution")
            logger.error(f"❌ SQL Parse Error: {str(e)}\nQuery that failed:\n{reviewed_sql}\n")
        
        if issues:
            logger.warning(f"⚠️ SQL Review found {len(issues)} issue(s): {issues}")
        else:
            logger.info(f"✅ SQL Review passed - query is valid")
        
        return {
            "valid": len(issues) == 0,
            "sql": reviewed_sql,  # Return reviewed SQL (with safety limit injected)
            "issues": issues,
            "suggestions": suggestions
        }
    
    async def _generate_sql(self, user_query: str, context: Dict = None) -> Optional[str]:
        # Create cache key from user query, context, and iteration to allow regeneration
        iteration = context.get("iteration", 0) if context else 0
        last_error = context.get("last_error") if context else None
        # Handle None last_error
        last_error_str = (last_error[:100] if last_error else "") if last_error is not None else ""
        # Include iteration and error in cache key to allow different SQL on retries
        cache_key = hashlib.md5(
            f"{user_query}_{iteration}_{last_error_str}_{json.dumps(context or {}, sort_keys=True)}".encode()
        ).hexdigest()
        
        # Check cache first (but allow regeneration if context changed)
        if cache_key in self.llm_response_cache and not last_error_str:
            logger.debug("Using cached LLM response")
            cached_response = self.llm_response_cache[cache_key]
            sql = self._extract_sql_from_text(cached_response)
            sql = self._fix_common_sql_issues(sql)
            return sql
        
        # Rate limiting: Add delay between LLM calls
        current_time = time.time()
        time_since_last_call = current_time - self._last_llm_call_time
        if time_since_last_call < self._llm_call_delay:
            delay_needed = self._llm_call_delay - time_since_last_call
            logger.debug(f"Rate limiting: waiting {delay_needed:.2f}s")
            await asyncio.sleep(delay_needed)
        
        # Build comprehensive schema info including relationships
        # Filter by allowed_tables if provided
        schema_info = ""
        if self.schema_inspector:
            schema_parts = []
            
            # If allowed_tables is provided, filter schema to only show allowed tables/columns
            if self.allowed_tables:
                filtered_tables = {}
                for table_name, table_info in self.allowed_tables.items():
                    if table_name in self.schema_inspector.tables:
                        allowed_cols = table_info.get("allowed_columns", [])
                        description = table_info.get("description", "")
                        # Filter columns to only allowed ones
                        all_cols = self.schema_inspector.tables[table_name]
                        if allowed_cols:
                            filtered_cols = [col for col in all_cols if col in allowed_cols]
                            filtered_tables[table_name] = filtered_cols
                        else:
                            filtered_tables[table_name] = all_cols
                
                # Build table info with descriptions
                table_info_list = []
                for table_name, columns in filtered_tables.items():
                    table_desc = self.allowed_tables[table_name].get("description", "")
                    table_info_list.append(f"{table_name}: {columns}" + (f" - {table_desc}" if table_desc else ""))
                schema_parts.append(f"Allowed Tables (with allowed columns and descriptions):\n" + "\n".join(table_info_list))
            else:
                # Use all tables if no restrictions
                schema_parts.append(f"Tables: {json.dumps(self.schema_inspector.tables, indent=2)}")
            
            # Filter relationships to only allowed tables
            if self.schema_inspector.relationships:
                relationships_str = []
                for table, rels in self.schema_inspector.relationships.items():
                    # Only include relationships for allowed tables
                    if not self.allowed_tables or table in self.allowed_tables:
                        for rel in rels:
                            # Only include if both tables are allowed
                            if not self.allowed_tables or (rel['from_table'] in self.allowed_tables and rel['to_table'] in self.allowed_tables):
                                relationships_str.append(f"{rel['from_table']}.{rel['from_column']} -> {rel['to_table']}.{rel['to_column']}")
                if relationships_str:
                    schema_parts.append(f"Relationships: {', '.join(relationships_str)}")
            
            # Filter primary keys to only allowed tables
            if self.schema_inspector.primary_keys:
                pk_str = []
                for table, pks in self.schema_inspector.primary_keys.items():
                    if not self.allowed_tables or table in self.allowed_tables:
                        pk_str.append(f"{table}: {', '.join(pks)}")
                if pk_str:
                    schema_parts.append(f"Primary Keys: {', '.join(pk_str)}")
            
            schema_info = "\n".join(schema_parts)
        else:
            schema_info = "No schema"
        
        # Build context for better SQL generation
        context_hint = ""
        if context and context.get("last_error"):
            error_msg = context['last_error'][:200]
            context_hint = f"\n\nIMPORTANT: Previous error - {error_msg}\nAnalyze this error and generate corrected SQL that avoids this issue."
        if context and context.get("previous_queries"):
            context_hint += f"\n\nPrevious queries tried:\n" + "\n".join([f"- {q[:100]}" for q in context['previous_queries'][-2:]])
        
        # Add discovered column values to context
        discovered_values_text = ""
        if context and context.get("discovered_column_values"):
            discovered_values = context.get("discovered_column_values", {})
            if discovered_values:
                discovered_values_text = "\n\nDISCOVERED COLUMN VALUES (Use these actual values in your query):\n"
                for col_key, values in discovered_values.items():
                    # Limit to first 30 values for prompt size
                    values_str = ', '.join([str(v) for v in values[:30]])
                    if len(values) > 30:
                        values_str += f" ... (and {len(values) - 30} more)"
                    discovered_values_text += f"- {col_key}: {values_str}\n"
                discovered_values_text += "\nIMPORTANT: Use these discovered values when filtering or matching. For example, if you see 'status' column with values ['active', 'inactive'], use WHERE status IN ('active', 'inactive') or WHERE status = 'active' based on the user query.\n"
        
        # Add previous results context for iterative reasoning
        previous_results_text = ""
        if context and context.get("previous_results"):
            prev_results = context.get("previous_results", [])
            if prev_results:
                previous_results_text = "\n\nPREVIOUS QUERY RESULTS (Use data from these to build next query):\n"
                for i, prev_result in enumerate(prev_results[-2:], 1):  # Last 2 results
                    if prev_result.get("success") and prev_result.get("data"):
                        rows = prev_result.get("data", [])[:5]  # First 5 rows
                        columns = prev_result.get("columns", [])
                        previous_results_text += f"\nQuery {i} Results ({prev_result.get('row_count', 0)} rows):\n"
                        previous_results_text += f"Columns: {', '.join(columns)}\n"
                        previous_results_text += f"Sample data: {json.dumps(rows, indent=2, default=str)}\n"
                previous_results_text += "\nIMPORTANT: Extract key values (IDs, names, etc.) from previous results and use them in WHERE clauses or JOIN conditions in your next query.\n"
        
        # Build instruction sections
        instruction_sections = []
        if self.db_instructions:
            instruction_sections.append(f"Database Instructions: {self.db_instructions}")
        if self.agent_instructions:
            instruction_sections.append(f"Agent Instructions: {self.agent_instructions}")
        if self.specific_filter_instructions:
            instruction_sections.append(f"Specific Filter Instructions: {self.specific_filter_instructions}")
        
        instructions_text = "\n".join(instruction_sections) if instruction_sections else ""
        
        # Add table/column restrictions if allowed_tables is provided
        restrictions_text = ""
        if self.allowed_tables:
            restrictions_text = "\n\nIMPORTANT RESTRICTIONS:\n"
            restrictions_text += "- You can ONLY use the tables and columns listed above\n"
            restrictions_text += "- Do NOT use any tables or columns that are not in the allowed list\n"
            restrictions_text += "- If a table is listed, you can only use the columns specified for that table\n"
        
        prompt = f"""You are a SQL Server T-SQL expert. Generate a SINGLE, COMPLETE, VALID T-SQL query.

User Question: {user_query}

{instructions_text}

Database Schema:
{schema_info}
{restrictions_text}
{discovered_values_text}
{previous_results_text}
{context_hint}

CRITICAL REQUIREMENTS:
1. Generate a COMPLETE, FINISHED SQL query - DO NOT truncate or leave incomplete
2. Ensure all quotes are closed, all JOINs have ON clauses, all WHERE conditions are complete
3. Use proper SQL Server T-SQL syntax (TOP not LIMIT, [] not backticks)
4. DO NOT use SQL variables (e.g., @VariableName) - SQL Server requires DECLARE statements which are not allowed. Use literal values or omit conditions that require variables.
5. **USE DISCOVERED ENUM VALUES**: If you see discovered column values above, USE THEM in your WHERE clauses. For example, if status column has values ['active', 'inactive'], use WHERE status = 'active' or WHERE status IN ('active', 'inactive') based on the user query.
6. **ENUM COLUMNS**: For columns like status, type, code, entity, category - ALWAYS use the actual discovered values, not guessed values. Check the "DISCOVERED COLUMN VALUES" section above.
7. **PERFORMANCE: ALWAYS USE TOP LIMITS**: Unless the user explicitly asks for ALL records, ALWAYS add TOP N to your queries:
   - For exploratory queries: Use TOP 100 or TOP 1000
   - For sample data: Use TOP 10 or TOP 50
   - For aggregations (COUNT, SUM, etc.): TOP is not needed as these return few rows
   - Example: SELECT TOP 100 * FROM table WHERE condition
   - NEVER write "SELECT * FROM table" without TOP - this could fetch millions of rows!
8. Return output in EXACTLY this format - NO OTHER TEXT:
   SQL:
   <complete SQL only - can be multiple lines>
9. DO NOT include explanations, markdown, comments, or reasoning AFTER the SQL
10. DO NOT wrap SQL in code blocks or quotes
11. Make sure the query is syntactically complete and can execute
12. If the query is complex, write it across multiple lines for readability
13. Include ALL parts: SELECT columns, FROM tables, JOINs with ON clauses, WHERE conditions, GROUP BY, ORDER BY, etc.

IMPORTANT: Write the ENTIRE query. Do not stop mid-query. Include all JOINs, WHERE clauses, and other parts even if the query is long. The query must be complete from SELECT to the end.
IMPORTANT: Always use discovered enum values from the "DISCOVERED COLUMN VALUES" section when filtering by enum-like columns (status, type, code, entity, etc.).
IMPORTANT: ALWAYS include TOP N in your SELECT statements to prevent fetching millions of rows. Use TOP 100, TOP 1000, or another appropriate limit based on the query purpose. Only skip TOP for aggregate queries (COUNT, SUM, MAX, MIN, AVG) or when user explicitly asks for "all records".

Return ONLY the SQL query in the format above:"""
        
        try:
            self._last_llm_call_time = time.time()
            response = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt="You are a SQL Server T-SQL expert. Generate COMPLETE, FINISHED SQL queries. Never truncate or leave queries incomplete. Return ONLY valid SQL - no explanations, no markdown, no comments, no semicolons. If you cannot generate valid SQL, return NULL.",
                max_tokens=4000  # Increase max_tokens to prevent truncation
            )
            
            # Cache the response
            self.llm_response_cache[cache_key] = response
            
            # Extract SQL from response using the improved extraction method
            # This handles multi-line SQL properly
            sql = self._extract_sql_from_text(response)
            sql = self._fix_common_sql_issues(sql)
            
            if sql:
                logger.info(f"📝 Generated SQL Query:\n{sql}\n")
            
            return sql
        except Exception as e:
            error_msg = str(e).lower()
            # Handle rate limit errors with exponential backoff
            if "rate limit" in error_msg or "429" in error_msg or "too many requests" in error_msg:
                logger.warning(f"⚠️ Rate limit hit: {e}. Waiting before retry...")
                await asyncio.sleep(2.0)  # Wait 2 seconds on rate limit
                # Retry once
                try:
                    await asyncio.sleep(1.0)  # Additional delay
                    response = await self.llm_provider.get_response(
                        transcript=prompt,
                        system_prompt="Return ONLY valid SQL, complete, no truncation, no comments, no markdown, no semicolons, no multiple queries. If cannot generate, return NULL."
                    )
                    self.llm_response_cache[cache_key] = response
                    sql = self._extract_sql_from_text(response)
                    sql = self._fix_common_sql_issues(sql)
                    return sql
                except Exception as retry_error:
                    logger.error(f"❌ Retry after rate limit also failed: {retry_error}")
                    raise
            else:
                raise

    async def _reason_about_next_step(self, user_query: str, all_results: List[Dict]) -> Dict[str, Any]:
        # Pass context including iteration, last error, and discovered values
        context = {
            "iteration": self.state.iteration,
            "last_error": self.state.last_error,
            "previous_queries": [q.get("sql", "") for q in self.state.executed_queries[-2:]],
            "discovered_column_values": self.state.discovered_column_values,
            "exploration_phase": self.state.exploration_phase,
            "previous_results": all_results[-3:] if all_results else []  # Last 3 results for context
        }
        
        # If we have successful results, evaluate first before generating new SQL
        successful_results = [r for r in all_results if r.get("success")]
        if successful_results:
            # Use evaluation to determine if goal is achieved
            evaluation = await self._evaluate_results(all_results, user_query)
            if evaluation.get("satisfied", False) and evaluation.get("confidence", 0.0) >= 0.7:
                return {
                    "thinking": f"Goal achieved - {evaluation.get('reason', '')}",
                    "goal_achieved": True,
                    "confidence": evaluation.get("confidence", 0.85),
                    "next_query": None,
                    "reasoning": evaluation.get("reason", "Goal achieved with successful query results")
                }
            elif self.state.iteration >= 10:
                # After 10 iterations, accept current results even if not perfect
                total_rows = sum(r.get("row_count", 0) for r in successful_results)
                return {
                    "thinking": f"Reached max iterations - accepting results ({total_rows} rows)",
                    "goal_achieved": True,
                    "confidence": 0.7,
                    "next_query": None,
                    "reasoning": "Maximum iterations reached"
                }
        
        # AUTONOMOUS EXPLORATION PHASE
        # Step 1: Only explore enum columns that are RELEVANT to the user query
        if self.schema_inspector and self.schema_inspector.tables:
            # Only identify enum columns mentioned in user query or likely to be used
            # Don't proactively explore ALL enum columns - only relevant ones
            ambiguous_columns = self._identify_ambiguous_columns(user_query, self.schema_inspector.tables, proactive=False)
            
            # Filter to only columns that are actually relevant to the query
            # Check if user query mentions status, type, entity, etc. or if we're likely to need them
            query_lower = user_query.lower()
            relevant_enum_terms = ['status', 'state', 'type', 'category', 'entity', 'code', 'role', 'level']
            query_mentions_enum = any(term in query_lower for term in relevant_enum_terms)
            
            # Only explore if:
            # 1. User query mentions enum-related terms, OR
            # 2. We're in early iterations and haven't explored anything yet, OR
            # 3. We have specific enum columns identified from the query
            should_explore = (
                (query_mentions_enum and ambiguous_columns) or
                (self.state.iteration == 0 and ambiguous_columns and len(self.state.discovered_column_values) == 0)
            )
            
            # If we have relevant enum-like columns to explore and conditions are met
            if should_explore and ambiguous_columns and self.state.iteration < 4 and self.state.exploration_phase in ["initial", "exploring"]:
                # Explore the first unexplored column
                table_name, column_name = ambiguous_columns[0]
                self.state.exploration_phase = "exploring"
                
                return {
                    "thinking": f"Exploring enum values for {table_name}.{column_name} to understand what types of values exist",
                    "goal_achieved": False,
                    "confidence": 0.3,
                    "next_query": f"SELECT DISTINCT TOP 50 [{column_name}] FROM [{table_name}] WHERE [{column_name}] IS NOT NULL ORDER BY [{column_name}]",
                    "reasoning": f"Need to understand what enum values exist in {table_name}.{column_name} before applying filters",
                    "exploration_query": True,
                    "table_name": table_name,
                    "column_name": column_name
                }
        
        # Step 2: Use LLM to reason about what data to gather next
        # If we're in exploration phase and have some data, use LLM to decide next step
        if self.state.exploration_phase == "exploring" and self.state.discovered_column_values:
            # Use LLM to reason about what to explore next or if we're ready to query
            reasoning_prompt = f"""You are an autonomous SQL agent. Analyze the user query and the data you've gathered so far.

User Query: {user_query}

Data Gathered So Far:
{json.dumps({k: v[:20] for k, v in self.state.discovered_column_values.items()}, indent=2, default=str)}

Previous Query Results:
{json.dumps([{"row_count": r.get("row_count", 0), "columns": r.get("columns", [])} for r in all_results[-2:]], indent=2, default=str)}

Schema Available:
{json.dumps({k: v[:10] for k, v in (self.schema_inspector.tables if self.schema_inspector else {}).items()}, indent=2)}

Decide what to do next:
1. If you need more column value exploration, specify which table.column to explore
2. If you have enough information, generate the actual SQL query to answer the user's question
3. If you need to gather small pieces of data first, generate a simple exploratory query

Return JSON:
{{
    "action": "explore_column" | "gather_data" | "final_query",
    "reasoning": "Why you're taking this action",
    "table_name": "table to explore (if action=explore_column)",
    "column_name": "column to explore (if action=explore_column)",
    "sql": "SQL query (if action=gather_data or final_query)",
    "explanation": "What this query will help us learn"
}}"""

            try:
                reasoning_response = await self.llm_provider.get_response(
                    transcript=reasoning_prompt,
                    system_prompt="You are an autonomous SQL agent. Analyze data and decide what to explore or query next. Return valid JSON only.",
                    max_tokens=1000
                )
                
                # Parse JSON response
                try:
                    # Extract JSON from response (might have markdown or extra text)
                    json_match = re.search(r'\{.*\}', reasoning_response, re.DOTALL)
                    if json_match:
                        reasoning_data = json.loads(json_match.group(0))
                        action = reasoning_data.get("action", "final_query")
                        
                        if action == "explore_column":
                            table_name = reasoning_data.get("table_name")
                            column_name = reasoning_data.get("column_name")
                            if table_name and column_name:
                                return {
                                    "thinking": reasoning_data.get("reasoning", f"Exploring {table_name}.{column_name}"),
                                    "goal_achieved": False,
                                    "confidence": 0.3,
                                    "next_query": f"SELECT DISTINCT TOP 50 [{column_name}] FROM [{table_name}] WHERE [{column_name}] IS NOT NULL ORDER BY [{column_name}]",
                                    "reasoning": reasoning_data.get("explanation", f"Need to understand values in {table_name}.{column_name}"),
                                    "exploration_query": True,
                                    "table_name": table_name,
                                    "column_name": column_name
                                }
                        
                        elif action == "gather_data":
                            sql = reasoning_data.get("sql")
                            if sql and self._is_valid_sql(sql):
                                self.state.exploration_phase = "querying"
                                return {
                                    "thinking": reasoning_data.get("reasoning", "Gathering initial data"),
                                    "goal_achieved": False,
                                    "confidence": 0.4,
                                    "next_query": sql,
                                    "reasoning": reasoning_data.get("explanation", "Gathering data to understand the structure"),
                                    "exploration_query": False
                                }
                        
                        elif action == "final_query":
                            sql = reasoning_data.get("sql")
                            if sql and self._is_valid_sql(sql):
                                self.state.exploration_phase = "querying"
                                return {
                                    "thinking": reasoning_data.get("reasoning", "Building final query with discovered information"),
                                    "goal_achieved": False,
                                    "confidence": 0.6,
                                    "next_query": sql,
                                    "reasoning": reasoning_data.get("explanation", "Using gathered data to answer the question"),
                                    "exploration_query": False
                                }
                except json.JSONDecodeError:
                    logger.warning("Could not parse LLM reasoning response as JSON")
            except Exception as e:
                logger.error(f"Error in autonomous reasoning: {e}")
        
        # Step 3: Generate SQL query with discovered context
        # Update exploration phase if we have enough data or if we've explored enough
        if self.state.exploration_phase == "exploring":
            # Move to querying phase if:
            # 1. We have discovered values, OR
            # 2. We've explored 2+ columns, OR
            # 3. User query doesn't mention enum-related terms
            query_lower = user_query.lower()
            enum_terms = ['status', 'state', 'type', 'category', 'entity', 'code']
            if (self.state.discovered_column_values or 
                len(self.state.explored_columns) >= 2 or 
                not any(term in query_lower for term in enum_terms)):
                self.state.exploration_phase = "querying"
                logger.info(f"🔄 Moving from exploration to querying phase")
        
        sql = await self._generate_sql(user_query, context)
        
        if not sql:
            # If SQL generation failed, try to move forward anyway
            if self.state.exploration_phase == "exploring":
                self.state.exploration_phase = "querying"
        
        return {
            "thinking": f"Generated SQL query using discovered column values (iteration {self.state.iteration})",
            "goal_achieved": False,
            "confidence": 0.5,
            "next_query": sql,
            "reasoning": "Using gathered information to build the query"
        }

    def _classify_intent(self, user_query: str) -> str:
        """
        Classify user query intent to improve confidence scoring
        Returns: 'COUNT', 'EXISTS', 'LIST', 'DETAIL', 'METADATA', 'UNKNOWN'
        """
        query_lower = user_query.lower()
        
        # COUNT queries
        if any(word in query_lower for word in ['how many', 'count', 'number of', 'total']):
            return 'COUNT'
        
        # EXISTS/BOOLEAN queries
        if any(word in query_lower for word in ['does', 'is there', 'exist', 'has', 'contains', 'any']):
            return 'EXISTS'
        
        # LIST queries
        if any(word in query_lower for word in ['list', 'show', 'all', 'get all', 'find all']):
            return 'LIST'
        
        # DETAIL queries (specific entity)
        if any(word in query_lower for word in ['details', 'information about', 'tell me about', 'what is']):
            return 'DETAIL'
        
        # METADATA queries
        if any(word in query_lower for word in ['schema', 'structure', 'tables', 'columns', 'what tables']):
            return 'METADATA'
        
        return 'UNKNOWN'

    async def _evaluate_results(self, results: List[Dict], user_query: str) -> Dict[str, Any]:
        # Count ALL successful queries (including 0-row results)
        successful = [r for r in results if r.get("success")]
        total_rows = sum(r.get("row_count", 0) for r in successful)
        successful_with_data = [r for r in successful if r.get("row_count", 0) > 0]
        
        # Classify intent for semantic confidence scoring
        intent = self._classify_intent(user_query)
        
        # If we have successful queries, evaluate based on intent and data
        if successful:
            # Semantic satisfaction - not just row count
            if intent in ['COUNT', 'EXISTS']:
                # COUNT/EXISTS queries: 0 rows might be valid (count=0, doesn't exist)
                # But if we got a result, it's definitely correct
                if total_rows > 0:
                    confidence = 0.85
                else:
                    # 0 rows for COUNT/EXISTS is still valid (count=0 or doesn't exist)
                    confidence = 0.85
                return {
                    "satisfied": True,
                    "confidence": confidence,
                    "reason": f"Query executed successfully ({intent} intent: {total_rows} rows)",
                    "intent": intent
                }
            elif intent == 'METADATA':
                # Metadata queries: any result is good
                confidence = 0.85
                return {
                    "satisfied": True,
                    "confidence": confidence,
                    "reason": f"Metadata query executed successfully",
                    "intent": intent
                }
            elif total_rows > 0:
                # LIST/DETAIL queries: we have data - goal likely achieved
                # More lenient confidence for LIST queries - if we have results, we're likely done
                # Base confidence of 0.7 for any results, plus bonus for more rows
                confidence = min(0.9, 0.7 + min(total_rows / 100.0, 0.2))
                return {
                    "satisfied": True,
                    "confidence": confidence,
                    "reason": f"Found {total_rows} rows across {len(successful_with_data)} successful queries",
                    "intent": intent
                }
            else:
                # Query executed successfully but 0 rows - might be correct (no matching data)
                # For LIST/DETAIL, 0 rows might mean "not found" which is still valid
                confidence = 0.75
                return {
                    "satisfied": True,  # Accept 0 rows as valid result
                    "confidence": confidence,
                    "reason": f"Query executed successfully but returned 0 rows (no matching data found)",
                    "intent": intent
                }
        
        # No successful queries
        return {
            "satisfied": False,
            "confidence": 0.0,
            "reason": "No successful queries executed",
            "intent": intent
        }

    async def run(self, user_query: str) -> AsyncGenerator[Dict, None]:
        if not self._initialized:
            yield event(AgentEvent.ERROR, "Agent not initialized")
            return

        self.state = AgentState(goal=user_query, exploration_phase="initial")
        # Reset exploration state for new query
        self.state.discovered_column_values = {}
        self.state.explored_columns = set()
        # Generate friendly initial message (like MySQL agent)
        initial_message = f"I'll search for information about \"{user_query}\" for you."
        yield event(AgentEvent.THOUGHT, initial_message)
        if self.schema_inspector and self.schema_inspector.tables:
            yield event(AgentEvent.SCHEMA, {"tables": self.schema_inspector.tables, "table_count": len(self.schema_inspector.tables)})
        all_results = []

        while not self.state.should_stop():
            self.state.iteration += 1
            
            # Add small delay between iterations to prevent rapid-fire LLM calls
            if self.state.iteration > 1:
                await asyncio.sleep(0.3)  # 300ms delay between iterations
            
            reasoning = await self._reason_about_next_step(user_query, all_results)
            self.state.plan.append(reasoning.get("thinking", ""))
            
            # Generate user-friendly explanation (like MySQL agent)
            thinking_explanation = await self._generate_thinking_explanation(user_query, reasoning, all_results)
            yield event(AgentEvent.THOUGHT, thinking_explanation or reasoning.get("thinking", "Planning..."))
            yield event(AgentEvent.PLAN, reasoning.get("reasoning", "Executing query"))
            
            # CRITICAL FIX: Check if goal is achieved from reasoning
            if reasoning.get("goal_achieved", False):
                self.state.confidence = reasoning.get("confidence", 0.85)
                logger.debug(f"Goal achieved (confidence: {self.state.confidence:.2f})")
                # Aggregate final results
                all_data = []
                total_rows = 0
                for r in all_results:
                    if r.get("success") and r.get("data"):
                        all_data.extend(r.get("data", []))
                        total_rows += r.get("row_count", 0)
                
                # Return raw results for main LLM to convert to natural language (no tool-side NL)
                raw_response = self._build_raw_result(all_results, total_rows)
                
                # Enhanced workflow trace (inspired by Veeam multi-agent architecture)
                workflow_trace = {
                    "total_iterations": self.state.iteration,
                    "successful_queries": len([q for q in self.state.executed_queries if q.get("success")]),
                    "failed_queries": len([q for q in self.state.executed_queries if not q.get("success")]),
                    "queries_executed": [{"sql": q.get("sql", ""), "success": q.get("success", False)} for q in self.state.executed_queries],
                    "errors_encountered": self.state.errors,
                    "final_confidence": self.state.confidence,
                    "goal_achieved": True
                }
                
                # Calculate total credits used
                total_credits_used = sum(usage.get("credit_cost", 0) for usage in self.credit_usage_list)
                
                yield event(AgentEvent.FINAL, {
                    "success": True,
                    "row_count": total_rows,
                    "answer": all_data,
                    "response": raw_response,  # Raw/original results for main LLM
                    "sql": [q.get("sql", "") for q in self.state.executed_queries if q.get("success")],
                    "confidence": self.state.confidence,
                    "iterations": self.state.iteration,
                    "workflow_trace": workflow_trace,  # Complete transparency (Veeam-inspired)
                    "credit_usage": {
                        "total_credits": total_credits_used,
                        "actions": self.credit_usage_list,
                        "breakdown": [
                            {
                                "action_code": usage.get("action_code"),
                                "credit_cost": usage.get("credit_cost", 0),
                                "action_id": usage.get("action_id")
                            }
                            for usage in self.credit_usage_list
                        ]
                    } if self.credit_usage_list else None
                })
                break  # Exit loop - goal achieved
            
            next_query = reasoning.get("next_query")
            
            if next_query:
                logger.info(f"💡 Generated Query from Reasoning:\n{next_query}\n")

            if not next_query or not self._is_valid_sql(next_query):
                error_msg = "Could not generate valid SQL"
                self.state.errors.append(error_msg)
                self.state.last_error = error_msg
                yield event(AgentEvent.ERROR, error_msg)
                continue

            # Semantic SQL hashing - normalize before hashing
            # BUT: Allow exploration queries to be retried if they failed (they're small and fast)
            normalized_sql = self._normalize_sql(next_query)
            sql_hash = hashlib.md5(normalized_sql.encode()).hexdigest()
            
            # Check if this is an exploration query
            is_exploration = reasoning.get("exploration_query", False)
            
            # For exploration queries, only block if we've successfully explored this column
            if is_exploration:
                table_name = reasoning.get("table_name")
                column_name = reasoning.get("column_name")
                if table_name and column_name:
                    col_key = f"{table_name}.{column_name}"
                    # If we've already successfully explored this column, skip
                    if col_key in self.state.explored_columns:
                        logger.info(f"⏭️ Column {col_key} already explored, skipping exploration query")
                        # Move to next phase
                        self.state.exploration_phase = "querying"
                        continue
            else:
                # For non-exploration queries, check for duplicates
                if sql_hash in self.executed_sql_hashes:
                    error_msg = "Duplicate SQL detected - same query was already tried. Stopping to avoid infinite loop."
                    self.state.errors.append(error_msg)
                    self.state.last_error = error_msg
                    yield event(AgentEvent.ERROR, error_msg)
                    # Break the loop instead of continuing - we've already tried this query
                    break
            
            # Add to executed hashes (for exploration queries, this allows tracking but doesn't block retries if needed)
            self.executed_sql_hashes.add(sql_hash)

            # PROACTIVE ENUM EXPLORATION: Check if query uses enum-like columns that haven't been explored
            # Only do this if we haven't explored many columns yet and it's relevant
            if self.schema_inspector and self.schema_inspector.tables and len(self.state.explored_columns) < 3:
                enum_columns_in_query = self._identify_columns_in_query(next_query, self.schema_inspector.tables)
                # Filter to only columns that are actually used in WHERE clauses or filters
                relevant_enum_columns = []
                for table_name, column_name in enum_columns_in_query:
                    # Check if column is used in WHERE clause (likely to need enum values)
                    if f"[{column_name}]" in next_query.upper() or column_name.upper() in next_query.upper():
                        # Check if it's in a WHERE clause context
                        query_upper = next_query.upper()
                        where_pos = query_upper.find("WHERE")
                        if where_pos > 0:
                            # Column appears after WHERE - likely needs enum exploration
                            col_pos = query_upper.find(column_name.upper(), where_pos)
                            if col_pos > where_pos:
                                relevant_enum_columns.append((table_name, column_name))
                
                if relevant_enum_columns and self.state.iteration < 6:
                    # Found enum-like columns in query that haven't been explored - explore them first
                    table_name, column_name = relevant_enum_columns[0]
                    col_key = f"{table_name}.{column_name}"
                    
                    # Skip if already explored
                    if col_key not in self.state.explored_columns:
                        logger.info(f"🔍 Query uses enum column {col_key} in WHERE clause - exploring values first")
                        self.state.exploration_phase = "exploring"
                        
                        yield event(AgentEvent.THOUGHT, f"Before executing the query, I need to understand what values exist in {table_name}.{column_name}")
                        yield event(AgentEvent.PLAN, f"Exploring enum values for {table_name}.{column_name} to ensure accurate filtering")
                        
                        # Execute exploration query
                        exploration_sql = f"SELECT DISTINCT TOP 50 [{column_name}] FROM [{table_name}] WHERE [{column_name}] IS NOT NULL ORDER BY [{column_name}]"
                        
                        # Generate reasoning for exploration query
                        exploration_reasoning = f"I'm exploring the distinct values in {table_name}.{column_name} to understand what enum values exist in this column. This will help me build accurate filters for the main query."
                        exploration_explanation = f"Exploring enum values in {table_name}.{column_name}"
                        
                        # Emit tool_call event for exploration query (so it appears in tool.call.delta)
                        yield event(AgentEvent.TOOL_CALL, {
                            "sql": exploration_sql,
                            "purpose": f"Explore enum values in {table_name}.{column_name}",
                            "explanation": exploration_explanation,
                            "reasoning": exploration_reasoning,
                            "exploration_query": True,
                            "table_name": table_name,
                            "column_name": column_name
                        })
                        
                        loop_explore = asyncio.get_event_loop()
                        explore_result = await loop_explore.run_in_executor(None, self.executor.execute_query, exploration_sql)
                        
                        if explore_result.get("success") and explore_result.get("data"):
                            values = [row.get(column_name) for row in explore_result.get("data", []) if row.get(column_name) is not None]
                            self.state.discovered_column_values[col_key] = values
                            self.state.explored_columns.add(col_key)
                            logger.info(f"✅ Discovered {len(values)} enum values for {col_key}: {values[:10]}...")
                            yield event(AgentEvent.OBSERVATION, {
                                **explore_result,
                                "explanation": f"Discovered {len(values)} distinct values in {table_name}.{column_name}: {', '.join([str(v) for v in values[:10]])}{'...' if len(values) > 10 else ''}",
                                "exploration_query": True
                            })
                            # Continue to next iteration to regenerate SQL with discovered values
                            self.state.iteration += 1  # Increment to avoid infinite loop
                            continue
                        else:
                            logger.warning(f"⚠️ Could not explore {col_key}, proceeding with query anyway")
            
            # SQL Review Agent step (inspired by Veeam multi-agent architecture)
            # This provides structured validation feedback before execution
            review_result = await self._review_and_validate_sql(next_query, user_query)
            if not review_result.get("valid", False):
                issues = review_result.get("issues", [])
                suggestions = review_result.get("suggestions", [])
                error_msg = f"SQL review failed: {'; '.join(issues)}"
                self.state.errors.append(error_msg)
                self.state.last_error = error_msg
                yield event(AgentEvent.ERROR, {
                    "error": error_msg,
                    "issues": issues,
                    "suggestions": suggestions,
                    "review_step": True  # Indicates this is from review agent
                })
                # Try to auto-fix common issues or regenerate SQL
                logger.warning(f"SQL Review found issues: {len(issues)} issue(s)")
                continue

            # IMPORTANT: Use the reviewed SQL (which may have safety limits injected)
            reviewed_sql = review_result.get("sql", next_query)
            
            # Print query before execution
            logger.info(f"🔍 Executing SQL Query:\n{reviewed_sql}\n")
            
            # Generate detailed reasoning for why this query is being executed
            # Do this BEFORE emitting tool_call to ensure reasoning is ready
            query_reasoning = await self._generate_query_reasoning(
                reviewed_sql, 
                user_query, 
                context={
                    "discovered_column_values": self.state.discovered_column_values,
                    "previous_queries": [q.get("sql", "") for q in self.state.executed_queries[-2:]],
                    "previous_results": all_results[-2:] if all_results else []
                }
            )
            
            # Generate user-friendly action explanation (like MySQL agent)
            action_explanation = await self._generate_action_explanation(reviewed_sql, user_query)
            
            # Check credit balance before executing query
            # Use app_id if available, otherwise app_code will be used
            if (self.app_id or self.app_code) and self.token and self.company_id and self.user_id:
                try:
                    # Import credit service dynamically to avoid circular imports
                    import sys
                    import os
                    # Add backend path if not already there
                    backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'backend')
                    if backend_path not in sys.path:
                        sys.path.insert(0, backend_path)
                    
                    from services.credit_service import check_sufficient_credits
                    
                    # Check if company has sufficient credits
                    has_sufficient, balance, required = await check_sufficient_credits(
                        token=self.token,
                        company_id=self.company_id,
                        app_id=self.app_id,
                        action_code="execute_query",
                        app_code=self.app_code
                    )
                    
                    if not has_sufficient:
                        # Insufficient credits - return error
                        error_msg = f"Insufficient credits to execute query. Current balance: {balance}, Required: {required}. Please add credits to your account."
                        logger.error(f"❌ {error_msg}")
                        yield event(AgentEvent.ERROR, {
                            "error": "insufficient_credits",
                            "message": error_msg,
                            "current_balance": balance,
                            "required_credits": required
                        })
                        return
                except Exception as credit_check_error:
                    # Log but allow execution to proceed if credit check fails
                    logger.warning(f"Failed to check credit balance: {credit_check_error}, allowing execution to proceed")
            
            # CRITICAL: Emit tool_call event BEFORE executing the query
            # This ensures tool.call.delta is sent before tool.result.delta
            yield event(AgentEvent.TOOL_CALL, {
                "sql": reviewed_sql, 
                "purpose": user_query,
                "explanation": action_explanation,
                "reasoning": query_reasoning if query_reasoning else "Executing query to retrieve the requested information."  # Detailed reasoning for why this query is executed
            })
            
            # Now execute the query AFTER emitting tool_call
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.executor.execute_query, reviewed_sql)
            self.state.executed_queries.append({"sql": reviewed_sql, "success": result.get("success", False)})
            
            # Track credit usage for execute_query action (store for inclusion in final result)
            # Note: We track synchronously here to ensure it completes before FINAL event
            # Use app_id if available, otherwise app_code will be used
            if (self.app_id or self.app_code) and self.token and self.company_id and self.user_id:
                try:
                    # Import credit service dynamically to avoid circular imports
                    import sys
                    import os
                    # Add backend path if not already there
                    backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'backend')
                    if backend_path not in sys.path:
                        sys.path.insert(0, backend_path)
                    
                    from services.credit_service import track_action_credit
                    
                    logger.info(f"💰 Tracking credit usage for app_id={self.app_id}, app_code={self.app_code}, action_code=execute_query")
                    
                    # Track credit usage synchronously to ensure it completes
                    credit_info = await track_action_credit(
                        token=self.token,
                        company_id=self.company_id,
                        user_id=self.user_id,
                        app_id=self.app_id,
                        action_code="execute_query",
                        app_code=self.app_code,
                        agent_id=self.agent_id,
                        metadata={
                            "sql": reviewed_sql[:500],  # Truncate long SQL
                            "success": result.get("success", False),
                            "row_count": result.get("row_count", 0)
                        }
                    )
                    
                    if credit_info:
                        logger.info(f"✅ Credit tracked: {credit_info['credit_cost']} credits for action {credit_info['action_code']}")
                        self.credit_usage_list.append(credit_info)
                    else:
                        logger.warning(f"⚠️ Credit tracking returned None - credits may not have been tracked")
                except Exception as credit_error:
                    # Log but don't fail the query execution
                    logger.error(f"❌ Failed to track credit usage: {credit_error}")
                    import traceback
                    logger.error(traceback.format_exc())
            else:
                missing = []
                if not self.app_id:
                    missing.append("app_id")
                if not self.token:
                    missing.append("token")
                if not self.company_id:
                    missing.append("company_id")
                if not self.user_id:
                    missing.append("user_id")
                logger.warning(f"⚠️ Credit tracking skipped - missing: {', '.join(missing)}")
            
            # Handle exploration queries - store discovered column values
            if reasoning.get("exploration_query", False) and result.get("success"):
                table_name = reasoning.get("table_name")
                column_name = reasoning.get("column_name")
                if table_name and column_name and result.get("data"):
                    # Extract distinct values from the result
                    values = [row.get(column_name) for row in result.get("data", []) if row.get(column_name) is not None]
                    col_key = f"{table_name}.{column_name}"
                    self.state.discovered_column_values[col_key] = values
                    self.state.explored_columns.add(col_key)
                    logger.info(f"✅ Stored {len(values)} discovered values for {col_key}: {values[:10]}...")
                    # Update observation explanation for exploration
                    observation_explanation = f"Discovered {len(values)} distinct values in {table_name}.{column_name}: {', '.join([str(v) for v in values[:10]])}{'...' if len(values) > 10 else ''}"
                    # Move to querying phase after successful exploration
                    self.state.exploration_phase = "querying"
                else:
                    observation_explanation = await self._generate_observation_explanation(result, user_query)
                    # If exploration failed, still move forward to avoid getting stuck
                    if reasoning.get("exploration_query", False):
                        self.state.exploration_phase = "querying"
            else:
                # Generate user-friendly observation explanation (like MySQL agent)
                observation_explanation = await self._generate_observation_explanation(result, user_query)
            
            # Note: tool_call event was already emitted before query execution (line ~1542)
            
            yield event(AgentEvent.OBSERVATION, {
                **result,
                "explanation": observation_explanation,
                "exploration_query": reasoning.get("exploration_query", False)
            })
            self.state.observations.append(result)
            all_results.append(result)

            if result.get("success"):
                evaluation = await self._evaluate_results(all_results, user_query)
                self.state.confidence = evaluation.get("confidence", 0.0)
                yield event(AgentEvent.THOUGHT, f"Evaluation: {evaluation.get('reason', '')}")
                # For LIST/DETAIL queries, use lower threshold (0.65) to prevent duplicate queries
                # For other queries, use 0.7 threshold
                intent = evaluation.get("intent", "UNKNOWN")
                confidence_threshold = 0.65 if intent in ['LIST', 'DETAIL'] else 0.7
                if evaluation.get("satisfied", False) and self.state.confidence >= confidence_threshold:
                    all_data = []
                    total_rows = 0
                    for r in all_results:
                        if r.get("success") and r.get("data"):
                            all_data.extend(r.get("data", []))
                            total_rows += r.get("row_count", 0)
                    
                    # Return raw results for main LLM to convert to natural language (no tool-side NL)
                    raw_response = self._build_raw_result(all_results, total_rows)
                    
                    # Enhanced workflow trace (inspired by Veeam multi-agent architecture)
                    workflow_trace = {
                        "total_iterations": self.state.iteration,
                        "successful_queries": len([q for q in self.state.executed_queries if q.get("success")]),
                        "failed_queries": len([q for q in self.state.executed_queries if not q.get("success")]),
                        "queries_executed": [{"sql": q.get("sql", ""), "success": q.get("success", False)} for q in self.state.executed_queries],
                        "errors_encountered": self.state.errors,
                        "final_confidence": self.state.confidence,
                        "goal_achieved": True
                    }
                    
                    # Calculate total credits used
                    total_credits_used = sum(usage.get("credit_cost", 0) for usage in self.credit_usage_list)
                    
                    yield event(AgentEvent.FINAL, {
                        "success": True,
                        "row_count": total_rows,
                        "answer": all_data,
                        "response": raw_response,  # Raw/original results for main LLM
                        "sql": [q.get("sql", "") for q in self.state.executed_queries if q.get("success")],
                        "confidence": self.state.confidence,
                        "iterations": self.state.iteration,
                        "workflow_trace": workflow_trace,  # Complete transparency (Veeam-inspired)
                        "credit_usage": {
                            "total_credits": total_credits_used,
                            "actions": self.credit_usage_list,
                            "breakdown": [
                                {
                                    "action_code": usage.get("action_code"),
                                    "credit_cost": usage.get("credit_cost", 0),
                                    "action_id": usage.get("action_id")
                                }
                                for usage in self.credit_usage_list
                            ]
                        } if self.credit_usage_list else None
                    })
                    break
            else:
                error = result.get("error", "Unknown error")
                self.state.errors.append(error)
                self.state.last_error = error
                yield event(AgentEvent.ERROR, f"Query failed: {error}")

        if not all_results or not any(r.get("success") for r in all_results):
            # Return raw error info for main LLM to phrase (no tool-side natural language)
            raw_error = json.dumps({
                "success": False,
                "error": "Could not achieve goal",
                "errors": self.state.errors,
                "confidence": self.state.confidence
            }, default=str)
            yield event(AgentEvent.FINAL, {
                "success": False,
                "error": "Could not achieve goal",
                "response": raw_error,
                "confidence": self.state.confidence,
                "errors": self.state.errors
            })
    
    async def _generate_thinking_explanation(self, user_query: str, reasoning: Dict, all_results: List[Dict]) -> str:
        """
        Generate user-friendly thinking explanation (like MySQL agent)
        Examples: "I'll search for...", "Let me check...", "Now let me..."
        """
        if not reasoning.get("next_query"):
            # Goal achieved or no query needed
            if reasoning.get("goal_achieved"):
                return "I've found the information you requested."
            return "Analyzing your request..."
        
        # Generate explanation based on query type
        query_lower = user_query.lower()
        if "search" in query_lower or "find" in query_lower:
            return f"I'll search for \"{user_query}\" in the database."
        elif "list" in query_lower or "show" in query_lower or "all" in query_lower:
            return f"I'll retrieve the information you requested."
        elif "count" in query_lower or "how many" in query_lower:
            return f"I'll count the records for you."
        else:
            return f"I'll search for information about \"{user_query}\"."
    
    async def _generate_query_reasoning(self, sql: str, user_query: str, context: Dict = None) -> str:
        """
        Generate detailed reasoning for why this query is being executed
        Uses LLM to explain the strategy and approach
        """
        # Build context for reasoning
        context_info = ""
        if context:
            if context.get("discovered_column_values"):
                context_info += f"\nDiscovered Column Values: {json.dumps(context.get('discovered_column_values'), indent=2, default=str)}\n"
            if context.get("previous_queries"):
                context_info += f"\nPrevious Queries: {context.get('previous_queries', [])}\n"
            if context.get("previous_results"):
                prev_results = context.get("previous_results", [])
                if prev_results:
                    context_info += f"\nPrevious Results: {json.dumps([{'row_count': r.get('row_count', 0), 'columns': r.get('columns', [])} for r in prev_results[-2:]], indent=2, default=str)}\n"
        
        reasoning_prompt = f"""You are a SQL agent explaining your query strategy. Analyze why this query is being executed.

User Query: {user_query}

SQL Query to Execute:
{sql}
{context_info}

Explain in 2-3 sentences:
1. WHY this query is being executed (what information it will provide)
2. HOW it helps answer the user's question
3. What strategy/approach you're using (e.g., "First getting a count to understand the data distribution, then filtering for specific records")

Be specific and clear. Examples:
- "I'm executing this COUNT query first to understand how many records exist for each status, which will help me determine if there are any open tickets before fetching the detailed records."
- "I'm filtering by Status = 'Opened' because the user asked for open support tickets, and I discovered from the previous query that 'Opened' is a valid status value in the database."

Reasoning:"""

        try:
            reasoning = await self.llm_provider.get_response(
                transcript=reasoning_prompt,
                system_prompt="You are a helpful SQL agent explaining your query strategy. Provide clear, concise reasoning in 2-3 sentences.",
                max_tokens=300
            )
            return reasoning.strip() if reasoning else "Executing query to retrieve the requested information."
        except Exception as e:
            logger.error(f"Error generating query reasoning: {e}")
            # Fallback to simple explanation
            sql_upper = sql.upper()
            if "COUNT" in sql_upper and "GROUP BY" in sql_upper:
                return "Getting a count of records grouped by status to understand the data distribution before fetching detailed records."
            elif "WHERE" in sql_upper:
                return "Filtering records based on the specified conditions to answer your question."
            else:
                return "Executing query to retrieve the requested information."

    async def _generate_action_explanation(self, sql: str, user_query: str) -> str:
        """
        Generate user-friendly action explanation (like MySQL agent)
        Examples: "Let me check what tables exist first", "Searching in authors table"
        """
        sql_upper = sql.upper()
        
        # Detect action type from SQL
        if "INFORMATION_SCHEMA" in sql_upper or "sys.tables" in sql_upper:
            return "Let me check what tables are available in the database."
        elif "DESCRIBE" in sql_upper or "INFORMATION_SCHEMA.COLUMNS" in sql_upper:
            # Extract table name if possible
            table_match = re.search(r'FROM\s+(\w+)', sql_upper)
            if table_match:
                table = table_match.group(1)
                return f"Let me check the structure of the {table} table."
            return "Let me check the table structure."
        elif "SELECT" in sql_upper:
            # Extract table names
            from_match = re.search(r'FROM\s+(\w+)', sql_upper)
            if from_match:
                table = from_match.group(1)
                return f"Searching in the {table} table..."
            return "Executing query..."
        else:
            return "Processing your request..."
    
    async def _generate_observation_explanation(self, result: Dict, user_query: str) -> str:
        """
        Generate user-friendly observation explanation (like MySQL agent)
        Examples: "Found it!", "Good!", "I found X records"
        """
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            if "access denied" in error.lower() or "login failed" in error.lower():
                return "I'm unable to access the database due to authentication issues."
            elif "invalid object" in error.lower() or "table" in error.lower():
                return "The table or column doesn't exist in the database."
            else:
                return f"Query execution failed: {error[:100]}"
        
        row_count = result.get("row_count", 0)
        if row_count == 0:
            return "No matching records found."
        elif row_count == 1:
            return "Found it! Here's the information:"
        elif row_count <= 10:
            return f"I found {row_count} records:"
        else:
            return f"I found {row_count} records. Here are the results:"
    
    def _build_raw_result(self, results: List[Dict], total_rows: int) -> str:
        """
        Build raw/original result payload for the main LLM (no natural language).
        Main LLM will convert this to natural language in the final response.
        """
        payload = {
            "success": True,
            "row_count": total_rows,
            "total_queries": len(results),
            "results": []
        }
        for r in results:
            if r.get("success") and r.get("data") is not None:
                rows = r.get("data", [])
                # Limit rows for context size; main LLM can still summarize
                payload["results"].append({
                    "row_count": len(rows),
                    "columns": list(rows[0].keys()) if rows else [],
                    "rows": rows[:50],
                    "sql": r.get("sql", "")
                })
            else:
                payload["results"].append({
                    "success": False,
                    "error": r.get("error", "Unknown error")
                })
        return json.dumps(payload, indent=2, default=str)
