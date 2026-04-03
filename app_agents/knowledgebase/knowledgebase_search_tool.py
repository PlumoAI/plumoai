from __future__ import annotations

from backend.services.app_agents.base_tool_agent import BaseToolAgent

"""
Knowledgebase Search Tool
Enables agents to search company and employee knowledgebase using semantic search
The API handles embeddings internally using OpenAI
"""
import os
import logging
import httpx
import uuid
from typing import Dict, Any, Optional, List, AsyncGenerator
from datetime import datetime

logger = logging.getLogger(__name__)

COMPANY_URL = os.getenv("COMPANY_URL")


class AgentEvent:
    """Event types for agent communication"""
    THOUGHT = "thought"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    RESULT = "result"
    ERROR = "error"
    FINAL = "final"


def event(event_type: str, content: Any):
    """Create an event dictionary"""
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content
    }


class KnowledgebaseSearchTool(BaseToolAgent):
    """
    Tool for searching agent knowledgebase (company and employee)
    """
    
    def __init__(
        self,
        token: str,
        company_id: str,
        agent_id: str,
        llm_provider,
        company_sources: Optional[List[Dict]] = None,
        employee_sources: Optional[List[Dict]] = None,
        is_company_enabled: bool = False,
        is_employee_enabled: bool = False
    ):
        """
        Initialize Knowledgebase Search Tool
        
        Args:
            token: Authentication token
            company_id: Company ID
            agent_id: Agent ID
            llm_provider: LLM provider instance
            company_sources: List of company knowledgebase sources
            employee_sources: List of employee knowledgebase sources
            is_company_enabled: Whether company knowledgebase is enabled
            is_employee_enabled: Whether employee knowledgebase is enabled
        """
        self.token = token
        self.company_id = company_id
        self.agent_id = agent_id
        self.llm_provider = llm_provider
        self.company_sources = company_sources or []
        self.employee_sources = employee_sources or []
        self.is_company_enabled = is_company_enabled
        self.is_employee_enabled = is_employee_enabled
        
        logger.info(f"📚 Knowledgebase Search Tool initialized")
        logger.info(f"   Company KB: {len(self.company_sources)} sources (enabled: {is_company_enabled})")
        logger.info(f"   Employee KB: {len(self.employee_sources)} sources (enabled: {is_employee_enabled})")
        logger.info(f"   Semantic search powered by API (embeddings handled server-side)")
    
    def get_tool_responsibility(self) -> str:
        """Return tool responsibility description for LLM with available documents"""
        # Build document list
        doc_list = []
        
        if self.is_company_enabled and self.company_sources:
            doc_list.append(f"\n📚 Company Knowledge ({len(self.company_sources)} documents):")
            for doc in self.company_sources[:10]:  # Show up to 10 documents
                title = doc.get('title', 'Untitled')
                file_type = doc.get('file_type', '').upper()
                doc_list.append(f"  • {title} ({file_type})")
            if len(self.company_sources) > 10:
                doc_list.append(f"  ... and {len(self.company_sources) - 10} more documents")
        
        if self.is_employee_enabled and self.employee_sources:
            doc_list.append(f"\n📋 Personal Knowledge ({len(self.employee_sources)} documents):")
            for doc in self.employee_sources[:5]:  # Show up to 5 personal documents
                title = doc.get('title', 'Untitled')
                file_type = doc.get('file_type', '').upper()
                doc_list.append(f"  • {title} ({file_type})")
            if len(self.employee_sources) > 5:
                doc_list.append(f"  ... and {len(self.employee_sources) - 5} more documents")
        
        documents_section = "\n".join(doc_list) if doc_list else "\n(No documents currently available)"
        
        return f"""🧠 PRIMARY BRAIN MEMORY - Knowledge Base Search

═══════════════════════════════════════════════════════════
⚡ CHECK THIS TOOL FIRST for ANY informational query!
═══════════════════════════════════════════════════════════

This is your PRIMARY MEMORY - your knowledge base containing all uploaded documents,
policies, procedures, FAQs, guidelines, and company information.
{documents_section}

🎯 WHEN TO USE (Priority #1):
  ✅ ANY question asking for information, facts, or knowledge
  ✅ Policy questions (HR, break times, working hours, etc.)
  ✅ Procedure and process questions
  ✅ Company guidelines and documentation
  ✅ FAQs and general knowledge queries
  ✅ Historical information stored in documents
  ✅ ANY query that could be answered from stored knowledge

⚠️ WHEN NOT TO USE:
  ❌ Mathematical calculations (3+5=?)
  ❌ Creating visualizations/charts
  ❌ Real-time database queries (current sales data)
  ❌ External API calls
  ❌ File operations

💡 SEARCH STRATEGY:
  1. Use natural language queries (e.g., "break policy", "working hours")
  2. Tool will find semantically similar content chunks
  3. Results include full context with document titles, sections, and relevance scores
  4. Base your answer on the chunk contents returned

🔍 This tool uses semantic search (AI-powered) to find relevant information
even if exact keywords don't match - it understands meaning and context."""
    
    async def initialize(self):
        """Initialize the tool (no-op for knowledgebase search)"""
        pass
    
    async def run(
        self,
        query: str,
        search_scope: str = "all",
        max_results: int = 5,
        project_fid: Optional[int] = None
    ) -> AsyncGenerator[Dict, None]:
        """
        Search the knowledgebase using semantic search (embeddings handled by API)
        
        Args:
            query: Search query
            search_scope: Search scope ("all", "company", "employee")
            max_results: Maximum number of results to return
            project_fid: Optional project folder ID to filter results
        """
        logger.info(f"🔍 Searching knowledgebase: query='{query}', scope={search_scope}, max_results={max_results}")
        
        # Yield initial thought
        yield event(AgentEvent.THOUGHT, f"Searching knowledgebase for: '{query}'")
        
        try:
            if not COMPANY_URL:
                logger.error("COMPANY_URL not configured")
                yield event(AgentEvent.ERROR, "Knowledgebase search service not configured")
                return
            
            # Validate search scope
            if search_scope not in ["all", "company", "employee"]:
                search_scope = "all"
            
            # Determine which sources to search
            search_company = (search_scope in ["all", "company"]) and self.is_company_enabled
            search_employee = (search_scope in ["all", "employee"]) and self.is_employee_enabled
            
            if not search_company and not search_employee:
                logger.warning("No knowledgebase sources enabled for search")
                yield event(AgentEvent.ERROR, "No knowledgebase sources are enabled")
                return
            
            # Prepare search request
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/search"
            
            # Build document IDs list
            document_ids = []
            if search_company:
                document_ids.extend([src.get("agent_knowledgebase_id") for src in self.company_sources])
            if search_employee:
                document_ids.extend([src.get("agent_knowledgebase_id") for src in self.employee_sources])
            
            if not document_ids:
                logger.warning("No knowledgebase sources available")
                yield event(AgentEvent.ERROR, "No knowledgebase sources available")
                return
            
            # Prepare search payload
            search_payload = {
                "query": query,
                "document_ids": document_ids,
                "limit": max_results,
                "project_fid": project_fid
            }
            
            # Yield plan
            yield event(AgentEvent.PLAN, f"Searching {len(document_ids)} documents from {search_scope} knowledgebase")
            
            # Yield tool call info
            yield event(AgentEvent.TOOL_CALL, {
                "query": query,
                "document_count": len(document_ids),
                "max_results": max_results,
                "search_scope": search_scope
            })
            
            logger.debug(f"📤 Sending search request: {len(document_ids)} documents, limit={max_results}")
            
            # Make search request
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": f"[{self.company_id}]"
                    },
                    json=search_payload
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("type") == "success" and result.get("data"):
                        data = result.get("data", {})
                        search_results = data.get("results", [])
                        count = data.get("count", 0)
                        
                        logger.info(f"✅ Knowledgebase search completed: {count} chunk(s) found")
                        
                        # Yield observation about search results
                        yield event(AgentEvent.OBSERVATION, f"Found {count} relevant text chunks from {search_scope} knowledgebase")
                        
                        # Format results
                        formatted_results = []
                        for chunk in search_results:
                            # Calculate relevance score (0-100%)
                            distance = chunk.get("distance", 1)
                            relevance_score = round((1 - distance) * 100, 1)
                            
                            formatted_results.append({
                                "chunk_id": chunk.get("id"),
                                "chunk_index": chunk.get("chunk_index"),
                                "chunk_text": chunk.get("chunk_text"),
                                "chunk_type": chunk.get("chunk_type"),
                                "distance": distance,
                                "relevance_score": relevance_score,
                                "document_id": chunk.get("document_id"),
                                "title": chunk.get("title"),
                                "file_type": chunk.get("file_type"),
                                "source_path": chunk.get("source_path"),
                                "project_fid": chunk.get("project_fid"),
                                "section_path": chunk.get("section_path"),
                                "heading": chunk.get("heading"),
                                "heading_level": chunk.get("heading_level"),
                                "keywords": chunk.get("keywords"),
                                "parent_id": chunk.get("parent_id"),
                                "part_index": chunk.get("part_index"),
                                "total_parts": chunk.get("total_parts"),
                                "token_count": chunk.get("token_count"),
                                "start_position": chunk.get("start_position"),
                                "end_position": chunk.get("end_position")
                            })
                        
                        final_result = {
                            "success": True,
                            "query": query,
                            "results": formatted_results,
                            "total_results": count,
                            "search_scope": search_scope
                        }
                        yield event(AgentEvent.RESULT, final_result)
                    else:
                        logger.warning(f"Knowledgebase search returned no results: {result}")
                        yield event(AgentEvent.OBSERVATION, "No matching content found in knowledgebase")
                        yield event(AgentEvent.RESULT, {
                            "success": True,
                            "query": query,
                            "results": [],
                            "total_results": 0,
                            "search_scope": search_scope,
                            "message": "No results found"
                        })
                else:
                    logger.error(f"Knowledgebase search failed: {response.status_code} - {response.text}")
                    yield event(AgentEvent.ERROR, f"Search request failed: {response.status_code}")
                    
        except httpx.TimeoutException:
            logger.error("Timeout while searching knowledgebase")
            yield event(AgentEvent.ERROR, "Search request timed out")
        except httpx.RequestError as e:
            logger.error(f"Error searching knowledgebase: {str(e)}")
            yield event(AgentEvent.ERROR, f"Search request error: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error searching knowledgebase: {str(e)}")
            yield event(AgentEvent.ERROR, f"Unexpected error: {str(e)}")
    
    async def get_source_details(self, document_id: int) -> Dict[str, Any]:
        """
        Get details for a specific knowledgebase document
        """
        logger.info(f"📄 Getting details for knowledgebase document {document_id}")
        
        try:
            if not COMPANY_URL:
                logger.error("COMPANY_URL not configured")
                return {
                    "success": False,
                    "error": "Knowledgebase service not configured"
                }
            
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/{document_id}"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "companyIds": f"[{self.company_id}]"
                    }
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("type") == "success" and result.get("data"):
                        document_data = result.get("data")
                        logger.info(f"✅ Retrieved details for document {document_id}: {document_data.get('title')}")
                        return {
                            "success": True,
                            "document": document_data
                        }
                    else:
                        logger.warning(f"Failed to get document details: {result}")
                        return {
                            "success": False,
                            "error": "Document not found"
                        }
                else:
                    logger.error(f"Failed to get document details: {response.status_code} - {response.text}")
                    return {
                        "success": False,
                        "error": f"Request failed: {response.status_code}"
                    }
                    
        except Exception as e:
            logger.error(f"Error getting document details: {str(e)}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

