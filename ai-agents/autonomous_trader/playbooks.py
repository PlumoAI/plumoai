from __future__ import annotations

from typing import Any, Dict, List, Tuple


# Curated playbooks inspired by `tradermonty/claude-trading-skills`.
# Kept as data-only so it can be reused by other modules.
PLAYBOOKS: Dict[str, Dict[str, Any]] = {
    # Market analysis & research
    "sector-analyst": {
        "category": "market_analysis",
        "title": "Sector Analyst",
        "summary": "Sector rotation analysis and regime framing using breadth / participation concepts.",
        "typical_inputs": {"sector_data": "CSV/chart optional", "market_context": "optional notes"},
        "outputs": ["rotation_summary", "regime_assessment", "risk_notes", "scenarios"],
    },
    "breadth-chart-analyst": {
        "category": "market_analysis",
        "title": "Breadth Chart Analyst",
        "summary": "Interprets market breadth charts to assess market health and positioning.",
        "typical_inputs": {"chart_or_description": "breadth chart link/image description"},
        "outputs": ["breadth_phase", "signals", "tactical_outlook", "risk_notes"],
    },
    "technical-analyst": {
        "category": "technical_analysis",
        "title": "Technical Analyst",
        "summary": "Pure technical analysis: trend, S/R, patterns, momentum, scenario triggers.",
        "typical_inputs": {"ticker": "symbol", "timeframe": "daily/weekly", "chart_notes": "optional"},
        "outputs": ["trend", "levels", "setups", "invalidations", "scenarios"],
    },
    "market-news-analyst": {
        "category": "market_analysis",
        "title": "Market News Analyst",
        "summary": "Impact-ranked summary of recent market-moving events and implications.",
        "typical_inputs": {"topic": "macro/theme/ticker", "lookback_days": 10},
        "outputs": ["ranked_events", "drivers", "implications", "watchlist"],
    },
    "us-stock-analysis": {
        "category": "equity_research",
        "title": "US Stock Analysis",
        "summary": "Structured US equity research memo: fundamentals + technicals + bull/bear cases.",
        "typical_inputs": {"ticker": "symbol", "focus": "fundamental/technical/both"},
        "outputs": ["memo", "key_metrics", "thesis", "risks", "questions"],
    },
    "market-environment-analysis": {
        "category": "market_analysis",
        "title": "Market Environment Analysis",
        "summary": "Global macro snapshot: indices, FX, commodities, yields, sentiment; daily/weekly template.",
        "typical_inputs": {"scope": "global/us", "horizon": "daily/weekly"},
        "outputs": ["dashboard", "regime", "key_levels", "risk_notes"],
    },
    # Calendars
    "economic-calendar-fetcher": {
        "category": "calendar",
        "title": "Economic Calendar Fetcher",
        "summary": "Upcoming macro events checklist and impact assessment (CPI/NFP/FOMC...).",
        "typical_inputs": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "importance": "high/medium/low"},
        "outputs": ["events", "impact_assessment", "prep_notes"],
    },
    "earnings-calendar": {
        "category": "calendar",
        "title": "Earnings Calendar",
        "summary": "Upcoming earnings dates organized by day/timing and portfolio relevance.",
        "typical_inputs": {"tickers": ["optional list"], "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
        "outputs": ["calendar", "watchlist", "risk_notes"],
    },
    # Strategy & risk
    "scenario-analyzer": {
        "category": "strategy",
        "title": "Scenario Analyzer",
        "summary": "Builds forward scenarios from headlines and maps sector/stock implications.",
        "typical_inputs": {"headline_set": ["strings"], "horizon_months": 18},
        "outputs": ["scenarios", "probabilities", "implications", "hedges"],
    },
    "backtest-expert": {
        "category": "strategy",
        "title": "Backtest Expert",
        "summary": "Backtest design review: hypothesis, constraints, robustness checks, walk-forward testing.",
        "typical_inputs": {"strategy_idea": "text", "market": "asset class", "data_assumptions": "optional"},
        "outputs": ["test_plan", "pitfalls", "metrics", "next_steps"],
    },
    "position-sizer": {
        "category": "risk",
        "title": "Position Sizer",
        "summary": "Risk-based position sizing methods (fixed fractional, ATR-based, Kelly framing).",
        "typical_inputs": {"account_value": "number", "risk_per_trade_pct": "number", "entry": "number", "stop": "number"},
        "outputs": ["shares", "risk_budget", "constraints", "notes"],
    },
}


WORKFLOWS: Dict[str, Dict[str, Any]] = {
    "daily_market_monitoring": {
        "title": "Daily Market Monitoring",
        "steps": [
            ("economic-calendar-fetcher", "Check today’s high-impact macro events."),
            ("earnings-calendar", "Check notable earnings today/this week."),
            ("market-news-analyst", "Summarize the last 1–10 days of market-moving news."),
            ("breadth-chart-analyst", "Assess breadth for health/positioning."),
        ],
    },
    "weekly_strategy_review": {
        "title": "Weekly Strategy Review",
        "steps": [
            ("sector-analyst", "Review sector rotation / regime."),
            ("technical-analyst", "Confirm index trends and key levels."),
            ("market-environment-analysis", "Macro context + cross-asset confirmation."),
        ],
    },
    "individual_stock_research": {
        "title": "Individual Stock Research",
        "steps": [
            ("us-stock-analysis", "Write a structured research memo."),
            ("earnings-calendar", "Check upcoming earnings and timing risk."),
            ("market-news-analyst", "Scan recent company/sector catalysts."),
        ],
    },
    "strategy_validation": {
        "title": "Strategy Validation",
        "steps": [
            ("backtest-expert", "Turn idea into a robust test plan."),
            ("position-sizer", "Translate into risk rules & sizing constraints."),
        ],
    },
}


def list_playbooks() -> List[Dict[str, Any]]:
    return [
        {"code": code, "title": meta.get("title"), "category": meta.get("category"), "summary": meta.get("summary")}
        for code, meta in sorted(PLAYBOOKS.items(), key=lambda x: x[0])
    ]


def get_playbook(code: str) -> Dict[str, Any] | None:
    return PLAYBOOKS.get(code)


def list_workflows() -> List[Dict[str, Any]]:
    return [{"code": c, "title": w.get("title")} for c, w in sorted(WORKFLOWS.items(), key=lambda x: x[0])]


def build_workflow_steps(workflow_code: str, topic: str = "") -> List[Dict[str, Any]] | None:
    wf = WORKFLOWS.get(workflow_code)
    if not wf:
        return None
    steps: List[Dict[str, Any]] = []
    for idx, (skill_code, step_text) in enumerate(wf["steps"], start=1):
        steps.append(
            {
                "step": idx,
                "playbook_code": skill_code,
                "instruction": step_text + (f" Target: {topic}" if topic else ""),
            }
        )
    return steps

