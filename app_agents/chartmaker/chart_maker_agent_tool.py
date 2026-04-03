from __future__ import annotations

from backend.services.app_agents.base_tool_agent import BaseToolAgent

"""
Chart Maker Agent Tool
Autonomous chart visualization tool that:
1. Analyzes user requirements
2. Requests data from main agent/SQL agent
3. Determines appropriate chart type
4. Formats data according to fl_chart specifications
5. Streams structured chart output
"""
import logging
import json
import re
from typing import Dict, List, Any, Optional, AsyncGenerator
from datetime import datetime
from dataclasses import dataclass, field
import uuid
import asyncio

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    DATA_REQUEST = "data_request"
    CHART_ANALYSIS = "chart_analysis"
    CHART_GENERATION = "chart_generation"
    CHART_DELTA = "chart_delta"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any):
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content
    }


@dataclass
class ChartMakerState:
    """State for chart maker agent"""
    user_requirement: str = ""
    chart_type: Optional[str] = None
    data_request: Optional[str] = None
    received_data: List[Dict[str, Any]] = field(default_factory=list)
    chart_config: Dict[str, Any] = field(default_factory=dict)
    iteration: int = 0
    errors: List[str] = field(default_factory=list)
    
    def should_stop(self) -> bool:
        return (
            self.chart_config.get("complete", False) or
            len(self.errors) > 3 or
            self.iteration >= 5
        )


class ChartMakerAgentTool(BaseToolAgent):
    """
    Chart Maker Agent Tool
    
    Analyzes requirements, requests data, and generates fl_chart formatted charts
    """
    
    # Tool responsibility and description (used for tool selection)
    TOOL_NAME = "Chart Tool"
    TOOL_DESCRIPTION = """The Chart Maker Tool generates visual charts from structured data to help users quickly understand trends, comparisons, and distributions. 

RESPONSIBILITIES:
- Analyze user requirements to determine appropriate chart type (bar, line, pie, scatter, radar, candlestick)
- Request or receive data from other tools (e.g., SQL queries, API responses)
- Format data according to fl_chart specifications
- Generate complete chart configurations with proper labels, colors, and formatting
- Support multiple chart types: bar charts for comparisons, line charts for trends, pie charts for proportions, scatter for correlations, radar for multi-dimensional data, candlestick for financial data

USE THIS TOOL WHEN:
- User requests a chart, graph, or visualization
- User wants to visualize data trends, comparisons, or distributions
- User asks to "show as chart", "create a graph", "visualize data", "bar chart", "pie chart", etc.
- Data needs to be presented visually for better understanding

DO NOT USE THIS TOOL WHEN:
- User only wants raw data or text reports
- No visualization is requested
- Data is not available or not structured"""
    
    # Supported chart types
    CHART_TYPES = {
        "line": "LineChart",
        "bar": "BarChart",
        "pie": "PieChart",
        "scatter": "ScatterChart",
        "radar": "RadarChart",
        "candlestick": "CandlestickChart"
    }
    
    @classmethod
    def get_tool_responsibility(cls) -> str:
        """
        Get tool responsibility description (to be appended to custom_description)
        
        Returns:
            Tool responsibility description string
        """
        return cls.TOOL_DESCRIPTION
    
    def __init__(
        self,
        llm_provider,
        main_agent=None,  # Reference to main agent for data requests
        sql_agent=None,  # Reference to SQL agent for database queries
        agent_instructions: Optional[str] = None
    ):
        """
        Initialize Chart Maker Agent Tool
        
        Args:
            llm_provider: LLM provider instance for reasoning
            main_agent: Optional reference to main agent for data requests
            sql_agent: Optional reference to SQL agent for database queries
            agent_instructions: Optional instructions for the chart agent
        """
        self.llm_provider = llm_provider
        self.main_agent = main_agent
        self.sql_agent = sql_agent
        self.agent_instructions = agent_instructions or "Generate accurate and visually appealing charts"
        self.state = ChartMakerState()
        self._initialized = False
    
    async def initialize(self):
        """Initialize the chart maker agent"""
        self._initialized = True
        logger.debug("Chart Maker Agent initialized")
    
    async def cleanup(self):
        """Cleanup the chart maker agent"""
        self._initialized = False
        logger.debug("Chart Maker Agent cleaned up")

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        """
        Stub implementation to satisfy the BaseToolAgent contract.

        The full chart generation pipeline has not yet been fully migrated into this
        plugin module. Until that work is complete, this tool will report that it is
        disabled instead of silently doing nothing or partially working.
        """
        err = {
            "success": False,
            "error": "ChartMakerAgentTool.run is not yet fully implemented in plugin form",
        }
        yield event(AgentEvent.ERROR, err)
        yield event(AgentEvent.FINAL, err)

