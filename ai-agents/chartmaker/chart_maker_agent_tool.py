from __future__ import annotations

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

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


def _split_into_chunks(text: str, chunk_size: int, overlap: int) -> List[str]:
    """
    Split text into overlapping chunks of at most chunk_size characters.
    Each chunk (except the last) ends at the nearest newline before the size
    limit so records that span the boundary appear in both chunks and aren't
    silently lost. Overlap carries the tail of the previous chunk into the
    next one for the same reason.
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            # Try to break at a newline so we don't cut mid-record
            newline_pos = text.rfind('\n', start, end)
            if newline_pos > start:
                end = newline_pos + 1
        chunk = text[start:end]
        chunks.append(chunk)
        # Next chunk starts overlap chars before the end so boundary records
        # appear in both chunks
        start = end - overlap if end - overlap > start else end

    return chunks


def _parse_json_array_tolerant(text: str) -> Optional[List[Dict]]:
    """
    Parse a JSON array from LLM output tolerantly:
    1. Try direct parse (whole response is valid JSON array).
    2. Try extracting the first complete [...] block.
    3. Try repairing a truncated array — the LLM hit a token limit and the
       response ends mid-element (no closing `]`). Find the last fully
       complete object `}` and close the array there.
    Returns a list of dicts, or None if nothing parseable.
    """
    # Attempt 1: full response is already a valid JSON array
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 2: extract the first [...] block (handles prose prefix/suffix)
    bracket_match = re.search(r'\[.*\]', text, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group())
            if isinstance(result, list):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Attempt 3: response is a truncated array (no closing ]) — repair it
    # by trimming to the last complete object and appending ]
    open_bracket = text.find('[')
    if open_bracket != -1:
        partial = text[open_bracket:]
        # Find the last complete object: rightmost `}` followed only by whitespace/commas
        last_close = partial.rfind('}')
        if last_close != -1:
            repaired = partial[:last_close + 1] + ']'
            try:
                result = json.loads(repaired)
                if isinstance(result, list) and len(result) > 0:
                    logger.info(f"📊 Repaired truncated JSON array — recovered {len(result)} records")
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

    return None


class ChartMakerAgentTool(BaseToolAgent):
    """
    Chart Maker Agent Tool
    
    Analyzes requirements, requests data, and generates fl_chart formatted charts
    """
    
    # Tool responsibility and description (used for tool selection)
    TOOL_NAME = "Chart Tool"
    TOOL_DESCRIPTION = """The Chart Maker Tool generates visual charts from structured data to help users quickly understand trends, comparisons, and distributions.

RESPONSIBILITIES:
- Analyze user requirements to determine appropriate chart type (bar, horizontal_bar, line, pie, scatter, radar, candlestick)
- Receive structured data and format it according to fl_chart specifications
- Generate complete chart configurations with proper labels, colors, and formatting
- Support multiple chart types: bar/horizontal_bar charts for comparisons, line charts for trends, pie charts for proportions, scatter for correlations, radar for multi-dimensional data, candlestick for financial data

USE THIS TOOL WHEN:
- User requests a chart, graph, or visualization
- User wants to visualize data trends, comparisons, or distributions
- User asks to "show as chart", "create a graph", "visualize data", "bar chart", "pie chart", etc.
- Data needs to be presented visually for better understanding

PASSING DATA TO THIS TOOL:
- If the data to visualize is already available (from the user message, a prior conversation turn, or a prior step result),
  pass it directly in tool_args as: "data": [{"label": "...", "value": ...}, ...]
- Do NOT call this tool with only a text query when you already have the structured values — include the data inline.
- If data must be fetched first, call the appropriate data tool before this one and pass its result here.

DO NOT USE THIS TOOL WHEN:
- User only wants raw data or text reports
- No visualization is requested
- Data is not available or not structured"""
    
    # Supported chart types
    CHART_TYPES = {
        "line": "LineChart",
        "bar": "BarChart",
        "horizontal_bar": "HorizontalBarChart",
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

    async def _normalize_provided_data(
        self,
        provided_data: Any,
        user_query: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Normalize whatever the brain passes as provided_data into a clean list of dicts
        suitable for charting.

        Three cases:
          1. Already a list of dicts with numeric values  →  return as-is.
          2. A brain-context dict (keys like _steps, result, response, query)
             →  extract the richest text field and use LLM to parse structured records.
          3. Anything else  →  return None (caller will fall back to analysis mode).
        """
        if not provided_data:
            return None

        # Case 1a: already flat {label, value} per row — return as-is.
        # Case 1b: multi-series list: 1 string label column + 2+ numeric columns
        #          (e.g. [{"province":"Punjab","Male":57109844,"Female":52902598},...])
        #          Keep the structure intact so the chart builder can produce grouped bars.
        if isinstance(provided_data, list) and provided_data and isinstance(provided_data[0], dict):
            first = provided_data[0]
            brain_keys = {"_steps", "result", "response", "query", "success", "citations"}
            real_keys = set(first.keys()) - brain_keys
            numeric_count = sum(1 for k in real_keys if isinstance(first.get(k), (int, float)))
            string_count  = sum(1 for k in real_keys if isinstance(first.get(k), str))
            if numeric_count >= 1 and string_count >= 1 and not (brain_keys & set(first.keys())):
                kind = "multi-series" if numeric_count > 1 else "simple {label,value}"
                logger.info(f"📊 Data is already structured ({kind}, {len(provided_data)} rows) — skipping LLM reshape")
                return provided_data

        # For all other shapes (nested dict, multi-column list, plain string, brain-context
        # dict, etc.) convert to a JSON string so the LLM can reshape it appropriately.
        text_content = None

        if isinstance(provided_data, str) and len(provided_data) > 20:
            # Plain string (e.g. comma-separated "State: value, State: value")
            text_content = provided_data

        elif isinstance(provided_data, (dict, list)):
            # Structured data that needs reshaping — serialise to JSON for the LLM
            try:
                text_content = json.dumps(provided_data, default=str)
            except Exception:
                text_content = str(provided_data)

        # Fallback: pull richest text field from a brain-context dict
        if not text_content and isinstance(provided_data, dict):
            for key in ("result", "response", "answer", "content", "text"):
                val = provided_data.get(key)
                if isinstance(val, str) and len(val) > 50:
                    text_content = val
                    break

        if not text_content:
            return None

        # ── Chunked LLM extraction ────────────────────────────────────────────
        # Split large payloads into overlapping chunks so no data is lost to a
        # token limit. Each chunk is processed independently; results are merged
        # (deduplicated by label) into a single complete record set.
        CHUNK_SIZE   = 1500   # chars per chunk — smaller chunks = shorter LLM output = no truncation
        OVERLAP      = 150    # trailing overlap so records split across boundaries aren't lost
        all_records: List[Dict] = []
        seen_labels: set = set()

        chunks = _split_into_chunks(text_content, CHUNK_SIZE, OVERLAP)
        total_chunks = len(chunks)
        logger.info(f"📊 Processing {len(text_content)} chars in {total_chunks} chunk(s) via LLM...")

        system_prompt = (
            "You are an intelligent data reshaping engine. "
            "You receive raw data in any form and convert it into a JSON array of chart-ready records. "
            "You automatically detect the structure of the input and preserve multi-series groupings — "
            "never flattening what should stay grouped. "
            "Output ONLY the JSON array or NO_DATA — nothing else."
        )

        for chunk_idx, chunk in enumerate(chunks):
            chunk_label = f"chunk {chunk_idx + 1}/{total_chunks}"
            logger.info(f"📦 [{chunk_label}] input ({len(chunk)} chars): {repr(chunk[:300])}")
            prompt = f"""Convert the source data below into a JSON array of chart-ready records.

User's chart request: "{user_query}"

Source data ({chunk_label}):
{chunk}

INSTRUCTIONS:
1. Identify the natural grouping structure of the data: what is the category/dimension (the X-axis), and what are the numeric measurements per category.
2. Output one JSON object per category. Each object must have:
   - exactly one string key for the category label (use the most natural name from the data, e.g. "label", or the actual column name)
   - one numeric key per measurement, using the exact measurement name from the source (never rename or merge them)
3. If there is only one numeric measurement per category, include just that one numeric key.
4. Never collapse multiple distinct measurements into a single "value" key unless the source genuinely has only one numeric series.
5. Normalise numbers: strip thousands separators, convert percentages to floats, drop unit suffixes (keep the coefficient).
6. This may be a partial chunk — extract every record present.

Output NO_DATA only if this chunk contains absolutely no numeric data."""

            try:
                logger.info(f"📦 [{chunk_label}] LLM prompt ({len(prompt)} chars): {repr(prompt[:400])}")
                response = await self.llm_provider.get_response(
                    transcript=prompt,
                    system_prompt=system_prompt,
                    max_tokens=4000,  # large output budget — JSON arrays can be long
                )
                logger.info(f"📦 [{chunk_label}] LLM output ({len(response) if response else 0} chars): {repr(response)[:400]}")

                if not response or "NO_DATA" in response.upper():
                    logger.info(f"📊 [{chunk_label}] no numeric data found")
                    continue

                cleaned = re.sub(r'^```[a-z]*\s*|\s*```$', '', response.strip(), flags=re.MULTILINE).strip()
                chunk_records = _parse_json_array_tolerant(cleaned)

                if not chunk_records:
                    logger.warning(f"📊 [{chunk_label}] could not parse JSON: {repr(cleaned)[:150]}")
                    continue

                added = 0
                for rec in chunk_records:
                    label = str(rec.get("label", "")).strip()
                    if label and label not in seen_labels:
                        seen_labels.add(label)
                        all_records.append(rec)
                        added += 1
                logger.info(f"📊 [{chunk_label}] added {added} new records (total so far: {len(all_records)})")

            except Exception as e:
                logger.warning(f"📊 [{chunk_label}] extraction failed: {e}")
                continue

        if not all_records:
            logger.info("📊 No records extracted from any chunk")
            return None

        # Reject if every value is zero across all records
        numeric_keys = [k for k in all_records[0] if isinstance(all_records[0][k], (int, float))]
        if numeric_keys and all(r.get(numeric_keys[0], 0) == 0 for r in all_records):
            logger.warning("📊 All extracted values are zero — discarding")
            return None

        logger.info(f"✅ Chunked extraction complete — {len(all_records)} total records")
        return all_records

    async def _analyze_requirement(
        self,
        user_query: str,
        available_data: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        prompt = f"""You are a chart visualization expert. Analyze the user's requirement and determine:

1. CHART TYPE: What type of chart best fits the requirement?
   - line: Trends over time, continuous data, time series
   - bar: Comparing categories vertically, discrete data, counts by group
   - horizontal_bar: Same as bar but horizontal — better for long category names or many items
   - pie: Proportions, percentages, parts of a whole
   - scatter: Correlation between two variables, relationships
   - radar: Multi-dimensional comparisons, multiple metrics
   - candlestick: Financial data (open, high, low, close)

2. DATA NEEDS: What specific data is required?
   - What entities/tables are mentioned? (e.g., "course enrollment" -> courses, enrollments)
   - What time period? (e.g., "in dec" -> December filter)
   - What metrics/columns? (e.g., count, amount, status)
   - What groupings/categories? (e.g., by status, by month, by category)

3. DATA QUERY: If database access is needed, what SQL query would retrieve this data?

User Requirement: "{user_query}"

Available Data (if any):
{json.dumps(available_data[:10], indent=2) if available_data else "No data provided yet - need to fetch from database"}

CRITICAL: Be specific about data requirements. Extract:
- Entity names (course, enrollment, ticket, user, etc.)
- Time references (dec, december, month, year, date range)
- Metrics (count, total, sum, average, etc.)
- Categories/groupings (by status, by type, by category, etc.)

Respond with JSON only (no other text):
{{
    "chart_type": "line|bar|horizontal_bar|pie|scatter|radar|candlestick",
    "data_needed": "detailed description of what data is needed",
    "data_query": "SQL query to retrieve the data (if needed, otherwise null)",
    "reasoning": "explanation of why this chart type fits",
    "needs_data": true/false
}}

Response:"""

        try:
            response = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt="You are a chart visualization expert. Analyze requirements dynamically and determine the best chart type and specific data needs."
            )

            if response:
                json_str = None
                code_block_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
                if code_block_match:
                    json_str = code_block_match.group(1)
                if not json_str:
                    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
                    if json_match:
                        json_str = json_match.group(0)
                if json_str:
                    try:
                        analysis = json.loads(json_str)
                        if "chart_type" in analysis and "data_needed" in analysis:
                            return analysis
                    except json.JSONDecodeError:
                        pass

            return {
                "chart_type": "bar",
                "data_needed": f"Data for: {user_query}",
                "data_query": None,
                "reasoning": "LLM analysis failed, using default",
                "needs_data": True,
                "error": "LLM analysis failed"
            }
        except Exception as e:
            logger.error(f"Error analyzing requirement: {e}")
            return {
                "chart_type": "bar",
                "data_needed": "Data required",
                "data_query": None,
                "reasoning": "Default chart type due to analysis error",
                "needs_data": True
            }

    async def _detect_data_fields(
        self,
        data: List[Dict[str, Any]],
        chart_type: str,
        user_query: str
    ) -> Dict[str, str]:
        if not data or not isinstance(data[0], dict):
            return {"x_field": None, "y_field": None, "label_field": None}

        sample_data = data[:3] if len(data) >= 3 else data
        all_keys = list(data[0].keys()) if data else []

        prompt = f"""Analyze the data structure and identify which fields should be used for chart axes.

Chart Type: {chart_type}
User Query: {user_query}

Sample Data:
{json.dumps(sample_data, indent=2, default=str)}

Available Fields: {', '.join(all_keys)}

Respond with JSON only:
{{
    "x_field": "field_name_for_x_axis" or null,
    "y_field": "field_name_for_y_axis" or null,
    "label_field": "field_name_for_labels" or null,
    "reasoning": "why these fields were chosen"
}}

Response:"""

        try:
            response = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt="You are a data analysis expert. Identify appropriate fields for chart axes."
            )
            if response:
                json_match = re.search(r'\{[^}]+\}', response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"Error detecting fields with LLM: {e}")

        numeric_fields, string_fields = [], []
        for key in all_keys:
            sample_value = data[0].get(key) if data else None
            if sample_value is not None:
                try:
                    float(str(sample_value).replace(',', '').strip())
                    numeric_fields.append(key)
                except (ValueError, TypeError):
                    if isinstance(sample_value, str) and len(sample_value) < 100:
                        string_fields.append(key)
                    elif isinstance(sample_value, (str, int, float)):
                        string_fields.append(key)

        y_field = next(
            (f for f in numeric_fields if any(p in f.lower() for p in ['count', 'total', 'sum', 'amount', 'value', 'number', 'quantity', 'score', 'rate'])),
            numeric_fields[0] if numeric_fields else None
        )
        x_field = next(
            (f for f in string_fields if any(p in f.lower() for p in ['name', 'category', 'label', 'title', 'type', 'date', 'time', 'month', 'year'])),
            string_fields[0] if string_fields else None
        )
        return {"x_field": x_field, "y_field": y_field, "label_field": x_field}

    async def _format_data_for_chart(
        self,
        data: List[Dict[str, Any]],
        chart_type: str,
        user_query: str = ""
    ) -> List[Dict[str, Any]]:
        if not data:
            return []

        field_mapping = await self._detect_data_fields(data, chart_type, user_query)
        x_field = field_mapping.get("x_field")
        y_field = field_mapping.get("y_field")
        label_field = field_mapping.get("label_field") or x_field
        formatted = []

        if chart_type == "line":
            for i, record in enumerate(data):
                if isinstance(record, dict):
                    x = record.get(x_field) if x_field else (record.get("x") or record.get("date") or i)
                    y = record.get(y_field) if y_field else (record.get("y") or record.get("value") or record.get("count") or 0)
                    if isinstance(x, str):
                        try:
                            x = float(x)
                        except:
                            x = i
                    formatted.append({"x": float(x) if isinstance(x, (int, float)) else float(i), "y": float(y) if isinstance(y, (int, float)) else 0.0})

        elif chart_type in ("bar", "horizontal_bar"):
            if not (data and isinstance(data[0], dict)):
                pass
            else:
                first = data[0]
                # Fully dynamic field resolution — driven only by the Python type
                # of each field's value in the first record. No hardcoded names.
                string_fields  = [k for k in first if isinstance(first[k], str)]
                numeric_fields = [k for k in first if isinstance(first[k], (int, float))]
                # Label = first string field; value(s) = all numeric fields
                label_col = string_fields[0] if string_fields else None
                logger.info(f"📊 {chart_type} field resolution — label='{label_col}', numeric={numeric_fields}")

                if chart_type == "bar" and len(numeric_fields) >= 2:
                    # Multi-series grouped bar
                    logger.info(f"📊 Multi-series bar — series={numeric_fields}")
                    for i, record in enumerate(data):
                        if isinstance(record, dict):
                            label_val = record.get(label_col) or f"Item {i+1}"
                            series_values = {
                                sf: float(record.get(sf, 0)) if isinstance(record.get(sf, 0), (int, float)) else 0.0
                                for sf in numeric_fields
                            }
                            formatted.append({"x": i, "label": str(label_val), "_series": series_values})
                else:
                    # Single-value bar or horizontal_bar
                    value_col = numeric_fields[0] if numeric_fields else None
                    for i, record in enumerate(data):
                        if isinstance(record, dict):
                            label_val = record.get(label_col) if label_col else f"Item {i+1}"
                            y = record.get(value_col) if value_col else 0
                            y_float = float(y) if isinstance(y, (int, float)) else 0.0
                            if isinstance(y, str):
                                try:
                                    y_float = float(y.replace(',', '').strip())
                                except (ValueError, TypeError):
                                    y_float = 0.0
                            if chart_type == "horizontal_bar":
                                formatted.append({"y": y_float, "label": str(label_val)})
                            else:
                                formatted.append({"x": i, "y": y_float, "label": str(label_val)})

        elif chart_type == "pie":
            colors = ["#FF6384", "#36A2EB", "#FFCE56", "#4BC0C0", "#9966FF", "#FF9F40", "#FF6384", "#C9CBCF"]
            total = sum([float(r.get(y_field) if y_field else r.get("value") or r.get("count") or r.get("amount") or 0) for r in data if isinstance(r, dict)])
            for i, record in enumerate(data):
                if isinstance(record, dict):
                    value = record.get(y_field) if y_field else (record.get("value") or record.get("count") or record.get("amount") or 0)
                    label = record.get(label_field) if label_field else (record.get("label") or record.get("name") or f"Item {i+1}")
                    color = record.get("color") or colors[i % len(colors)]
                    percentage = (float(value) / total * 100) if total > 0 else 0
                    formatted.append({"value": float(value) if isinstance(value, (int, float)) else 0.0, "title": str(label), "color": color, "radius": 50.0, "percentage": round(percentage, 1)})

        elif chart_type == "scatter":
            for i, record in enumerate(data):
                if isinstance(record, dict):
                    x = record.get(x_field) if x_field else (record.get("x") or record.get("x_value") or i)
                    y = record.get(y_field) if y_field else (record.get("y") or record.get("y_value") or record.get("value") or 0)
                    if isinstance(x, str):
                        try:
                            x = float(x)
                        except:
                            x = i
                    formatted.append({"x": float(x) if isinstance(x, (int, float)) else float(i), "y": float(y) if isinstance(y, (int, float)) else 0.0})

        elif chart_type == "radar":
            for i, record in enumerate(data):
                if isinstance(record, dict):
                    value = record.get(y_field) if y_field else (record.get("value") or record.get("score") or 0)
                    label = record.get(label_field) if label_field else (record.get("label") or record.get("name") or f"Dimension {i+1}")
                    formatted.append({"value": float(value) if isinstance(value, (int, float)) else 0.0, "label": str(label)})

        elif chart_type == "candlestick":
            for record in data:
                if isinstance(record, dict):
                    formatted.append({
                        "open": float(record.get("open", 0)),
                        "high": float(record.get("high", 0)),
                        "low": float(record.get("low", 0)),
                        "close": float(record.get("close", 0)),
                        "time": str(record.get("time") or record.get("date") or record.get("timestamp") or "")
                    })

        return formatted

    async def _generate_chart_title(
        self,
        chart_type: str,
        data: List[Dict[str, Any]],
        user_query: str,
    ) -> str:
        """Ask the LLM to produce a concise, descriptive chart title."""
        sample = data[:5] if data else []
        try:
            response = await self.llm_provider.get_response(
                transcript=(
                    f"Write a short, clear chart title (max 8 words) for a {chart_type} chart.\n"
                    f"User request: {user_query}\n"
                    f"Sample data labels: {[r.get('label', '') for r in sample]}\n\n"
                    "Rules:\n"
                    "- Title must describe WHAT is being shown (metric) and WHO/WHAT the categories are.\n"
                    "- Include time period or source if clearly present in the request (e.g. '2023 Census', '2011').\n"
                    "- Do NOT start with words like 'Chart of', 'Graph of', 'A bar chart', 'Generate', 'Create', 'Visualize'.\n"
                    "- Output the title text only — no quotes, no punctuation at the end."
                ),
                system_prompt="You write concise, descriptive chart titles. Output only the title text, nothing else.",
                max_tokens=50,
            )
            if response:
                title = response.strip().strip('"').strip("'")
                # Safety cap — if LLM still returns something long, trim at word boundary
                if len(title) > 80:
                    title = title[:77].rsplit(' ', 1)[0] + '...'
                logger.info(f"📊 Chart title: {repr(title)}")
                return title
        except Exception as e:
            logger.warning(f"📊 Title generation failed: {e}")

        # Fallback: clean up the raw query
        clean = re.sub(r'\[IDs from prior steps:.*?\]', '', user_query).strip()
        clean = re.sub(r'^(generate|create|make|build|show|give|produce|visualize|plot)\s+(a\s+)?(bar|pie|line|scatter|radar|candlestick)?\s*(chart|graph)?\s*(of|for|showing|with|using|based on)?\s*', '', clean, flags=re.IGNORECASE).strip()
        return (clean[:60] if len(clean) > 60 else clean) or "Chart"

    async def _generate_chart_config(
        self,
        chart_type: str,
        data: List[Dict[str, Any]],
        user_query: str
    ) -> Dict[str, Any]:
        if not isinstance(user_query, str):
            user_query = str(user_query) if user_query else ""

        formatted_data = await self._format_data_for_chart(data, chart_type, user_query)
        title = await self._generate_chart_title(chart_type, data, user_query)

        base_config = {
            "type": chart_type,
            "chartClass": self.CHART_TYPES.get(chart_type, "BarChart"),
            "title": title,
            "dataCount": len(formatted_data)
        }

        if chart_type == "line":
            y_values = [d.get("y", 0) for d in formatted_data if d.get("y") is not None]
            base_config.update({"spots": formatted_data, "showGrid": True, "showLabels": True, "curve": "linear", "minY": min(y_values) if y_values else 0, "maxY": max(y_values) if y_values else 100, "data": formatted_data})

        elif chart_type == "bar":
            bar_colors = [
                "#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF",
                "#FF9F40", "#E7E9ED", "#71B37C", "#F7464A", "#46BFBD",
                "#FDB45C", "#949FB1", "#4D5360", "#AC64AD", "#00AABB",
            ]
            bar_groups = []
            labels = []
            is_grouped = formatted_data and "_series" in formatted_data[0]

            if is_grouped:
                # Multi-series grouped bars — each x gets one bar per series
                series_fields = list(formatted_data[0]["_series"].keys())
                series_colors = bar_colors  # one color per series, consistent across all x positions
                legend = [{"label": sf, "color": series_colors[si % len(series_colors)]} for si, sf in enumerate(series_fields)]
                all_values = [v for dp in formatted_data for v in dp["_series"].values()]
                for i, dp in enumerate(formatted_data):
                    bars = [
                        {"fromY": 0, "toY": dp["_series"][sf], "color": series_colors[si % len(series_colors)], "width": 24}
                        for si, sf in enumerate(series_fields)
                    ]
                    bar_groups.append({"x": i, "barsSpace": 4, "bars": bars})
                    labels.append(dp.get("label") or f"Item {i+1}")
                base_config.update({
                    "barGroups": bar_groups, "labels": labels,
                    "legend": legend, "showGrid": True, "showLabels": True,
                    "minY": 0, "maxY": max(all_values) if all_values else 100,
                    "data": formatted_data,
                })
            else:
                # Single-value bars — unique color per bar
                y_values = [d.get("y", 0) for d in formatted_data if d.get("y") is not None]
                for i, dp in enumerate(formatted_data):
                    color = bar_colors[i % len(bar_colors)]
                    bar_groups.append({"x": dp.get("x", 0), "barsSpace": 4, "bars": [{"fromY": 0, "toY": dp.get("y", 0), "color": color, "width": 24}]})
                    labels.append(dp.get("label") or f"Item {int(dp.get('x', 0)) + 1}")
                base_config.update({
                    "barGroups": bar_groups, "labels": labels,
                    "showGrid": True, "showLabels": True,
                    "minY": min(y_values) if y_values else 0, "maxY": max(y_values) if y_values else 100,
                    "data": formatted_data,
                })

        elif chart_type == "horizontal_bar":
            colors = [
                "#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF",
                "#FF9F40", "#E7E9ED", "#71B37C", "#F7464A", "#46BFBD",
                "#FDB45C", "#949FB1", "#4D5360", "#AC64AD", "#00AABB",
            ]
            y_values = [d.get("y", 0) for d in formatted_data if d.get("y") is not None]
            labels = [d.get("label") or f"Item {i+1}" for i, d in enumerate(formatted_data)]
            data_entries = [{"y": d.get("y", 0)} for d in formatted_data]
            bar_colors_list = [colors[i % len(colors)] for i in range(len(formatted_data))]
            base_config.update({
                "labels": labels,
                "data": data_entries,
                "colors": bar_colors_list,
                "showGrid": True,
                "showLabels": True,
                "minY": 0,
                "maxY": max(y_values) if y_values else 100,
            })

        elif chart_type == "pie":
            total = sum([d.get("value", 0) for d in formatted_data])
            base_config.update({"sections": formatted_data, "showLabels": True, "showPercentage": True, "centerText": f"Total: {total}" if total > 0 else "", "data": formatted_data})

        elif chart_type == "scatter":
            x_values = [d.get("x", 0) for d in formatted_data if d.get("x") is not None]
            y_values = [d.get("y", 0) for d in formatted_data if d.get("y") is not None]
            base_config.update({"spots": formatted_data, "showGrid": True, "showLabels": True, "minX": min(x_values) if x_values else 0, "maxX": max(x_values) if x_values else 100, "minY": min(y_values) if y_values else 0, "maxY": max(y_values) if y_values else 100, "data": formatted_data})

        elif chart_type == "radar":
            values = [d.get("value", 0) for d in formatted_data if d.get("value") is not None]
            base_config.update({"entries": formatted_data, "maxValue": max(values) if values else 100, "showLabels": True, "data": formatted_data})

        elif chart_type == "candlestick":
            base_config.update({"candles": formatted_data, "showGrid": True, "showLabels": True, "data": formatted_data})

        return base_config

    async def _extract_data_from_query(self, user_query: str) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(user_query, str) or not user_query:
            return None
        try:
            extraction_prompt = f"""Extract data from the following query if it contains embedded data values.

Query: "{user_query}"

If data is found, convert it to a list of dicts with consistent keys.
If NO data found, respond with "NO_DATA".

Respond with JSON array only, or "NO_DATA":"""

            response = await self.llm_provider.get_response(
                transcript=extraction_prompt,
                system_prompt="You are a data extraction expert. Extract structured data from queries and convert to JSON format."
            )
            if response and "NO_DATA" not in response.upper():
                json_match = re.search(r'\[.*\]', response, re.DOTALL)
                if json_match:
                    try:
                        data = json.loads(json_match.group())
                        if data and isinstance(data, list) and len(data) > 0:
                            return data
                    except json.JSONDecodeError:
                        pass
            return None
        except Exception as e:
            logger.warning(f"Error extracting data from query: {e}")
            return None

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        self.state = ChartMakerState()

        # ── INCOMING DATA LOG ──────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info("📥 ChartMakerAgentTool.run() called")
        logger.info(f"   user_query      : {repr(user_query)[:300]}")
        logger.info(f"   provided_data   : type={type(provided_data).__name__}, "
                    f"value={json.dumps(provided_data, default=str)[:500] if provided_data is not None else 'None'}")
        logger.info(f"   session_id      : {session_id}")
        logger.info(f"   tool_args keys  : {list((tool_args or {}).keys())}")
        if tool_args:
            logger.info(f"   tool_args values: {json.dumps(tool_args, default=str)[:500]}")
        logger.info("=" * 60)
        # ──────────────────────────────────────────────────────────────────

        if not isinstance(user_query, str):
            user_query = str(user_query) if user_query else ""

        # tool_args["data"] always takes priority — if the brain explicitly passed data
        # inline, use it regardless of whether provided_data is also set.
        inline_data = (tool_args or {}).get("data")
        if inline_data:
            provided_data = inline_data
            logger.info(f"📊 tool_args.data present — using as primary data source (overrides provided_data)")

        # Normalize provided_data: handles brain-context dicts, plain text strings,
        # comma-string data, etc. — converts everything to clean list-of-dicts.
        if provided_data is not None:
            normalized = await self._normalize_provided_data(provided_data, user_query)
            if normalized:
                provided_data = normalized
                logger.info(f"📊 Normalized provided_data to {len(provided_data)} clean records")
            elif not isinstance(provided_data, list):
                # Could not extract structured records — clear so we enter awaiting_data
                # instead of charting a wrapper dict as a single meaningless bar
                logger.info("📊 Normalization returned None and provided_data is not a list — clearing to trigger awaiting_data")
                provided_data = None

        # Handle JSON chart spec passed as query string
        chart_spec = None
        try:
            if user_query.strip().startswith('{') and user_query.strip().endswith('}'):
                chart_spec = json.loads(user_query)
                if chart_spec.get('data') and not provided_data:
                    provided_data = chart_spec.get('data')
                user_query = chart_spec.get('title') or f"Create a {chart_spec.get('type', 'chart')} chart"
        except (json.JSONDecodeError, AttributeError):
            chart_spec = None

        self.state.user_requirement = user_query

        try:
            if not provided_data:
                provided_data = await self._extract_data_from_query(user_query)

            explicit_chart_type = None
            for chart_type in ["pie", "horizontal_bar", "horizontal bar", "bar", "line", "scatter", "radar", "candlestick"]:
                slug = chart_type.replace(" ", "_")
                if (chart_type in user_query.lower() or
                        f"{chart_type} chart" in user_query.lower() or
                        slug in user_query.lower()):
                    explicit_chart_type = slug  # normalise to underscore form
                    break

            if chart_spec:
                analysis = {"chart_type": chart_spec.get("type", "bar"), "data_needed": "Data provided in JSON specification", "data_query": None, "reasoning": "JSON spec", "needs_data": not bool(provided_data)}
            elif explicit_chart_type:
                analysis = {"chart_type": explicit_chart_type, "data_needed": f"Data for {explicit_chart_type} chart", "data_query": None, "reasoning": f"User requested {explicit_chart_type}", "needs_data": not bool(provided_data)}
            else:
                analysis = await self._analyze_requirement(user_query, provided_data)

            self.state.chart_type = analysis.get("chart_type", "bar")
            self.state.data_request = analysis.get("data_needed")

            if not provided_data and analysis.get("needs_data", True):
                data_needed = self.state.data_request or "structured numeric data"
                yield event(AgentEvent.CHART_ANALYSIS, {"chart_type": self.state.chart_type, "data_needed": data_needed, "data_query": analysis.get("data_query", ""), "reasoning": analysis.get("reasoning", ""), "needs_data": True, "status": "analysis_complete"})
                yield event(AgentEvent.FINAL, {
                    "success": False,
                    "chart_type": self.state.chart_type,
                    "data_needed": data_needed,
                    "data_query": analysis.get("data_query", ""),
                    "chart_config": None,
                    "status": "awaiting_data",
                    "response": f"Chart cannot be generated: the source data contains no actual numeric values. Need: {data_needed}. Please re-fetch data with explicit numbers.",
                })
                return

            if not provided_data:
                yield event(AgentEvent.ERROR, {"error": "No data provided for chart generation", "status": "error"})
                yield event(AgentEvent.FINAL, {"success": False, "error": "No data provided", "chart_config": None, "status": "error"})
                return

            data = provided_data if isinstance(provided_data, list) else [provided_data]
            if not data:
                yield event(AgentEvent.ERROR, {"error": "Empty data provided", "status": "error"})
                yield event(AgentEvent.FINAL, {"success": False, "error": "Empty data", "chart_config": None, "status": "error"})
                return

            self.state.received_data = data

            try:
                chart_config = await self._generate_chart_config(self.state.chart_type, self.state.received_data, user_query)
            except Exception as config_error:
                logger.error(f"Error generating chart config: {config_error}")
                yield event(AgentEvent.ERROR, {"error": f"Failed to generate chart configuration: {str(config_error)}", "status": "error"})
                yield event(AgentEvent.FINAL, {"success": False, "error": f"Chart generation failed: {str(config_error)}", "chart_config": None, "status": "error"})
                return

            chart_config["complete"] = True
            chart_config["status"] = "generated"
            self.state.chart_config = chart_config

            # Build the placeholder string the main agent embeds in its final response.
            # Format is self-contained so the frontend can detect and render it without
            # any chart-specific logic in the main agent brain.
            placeholder = "[[CHART_PLACEHOLDER " + json.dumps({
                "chartConfig": chart_config,
                "chartType": self.state.chart_type,
                "appCode": "chartmaker",
                "appName": "Chart Maker",
            }, separators=(",", ":")) + "]]"

            yield event(AgentEvent.CHART_DELTA, {"chart_config": chart_config, "complete": True, "status": "generated"})
            yield event(AgentEvent.FINAL, {
                "success": True,
                "chart_type": self.state.chart_type,
                "chart_config": chart_config,
                "data_points": len(data),
                "status": "generated",
                "response": placeholder,
            })

        except Exception as e:
            logger.error(f"Error in chart generation: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield event(AgentEvent.ERROR, {"error": str(e), "status": "error"})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "chart_config": None, "status": "error"})

