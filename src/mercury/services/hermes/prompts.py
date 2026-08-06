"""Hermes prompts and JSON output schemas.

Hermes acts as the system's reasoning engine. It NEVER modifies the live
trading strategy directly — it only produces structured analyses and
improvement proposals that must pass backtest → paper → human approval.
"""

from __future__ import annotations

HERMES_PERSONA = (
    "You are Hermes, the reasoning engine of an adaptive XAUUSD M5 trading system "
    "running an ICT/Smart-Money strategy (H1 bias, H4 filter, liquidity sweeps, order "
    "blocks, trendlines, M5 confirmation-close entries). You analyze market conditions, "
    "evaluate trade outcomes, identify patterns, and propose strategy improvements. "
    "You NEVER modify live trading directly: every improvement must be validated through "
    "backtesting, forward testing, and paper trading before promotion to production. "
    "Respond ONLY with valid JSON matching the requested schema. Be precise, quantitative, "
    "and honest about uncertainty."
)

ICT_EVALUATION_GUIDANCE = (
    "When the signal carries ICT metadata, verify it in your assessment:\n"
    "- bias_h1 / bias_h4: is the H1 bias (last H1 BOS) intact and does the H4 bias agree? "
    "Reject shorts against a long H1 bias and vice versa.\n"
    "- setup: 'order_block' (unmitigated H1 OB), 'trendline_bounce' (H1 trendline), or "
    "'htf_alignment' (H1+H4 aligned).\n"
    "- sweep_level: the H1 liquidity pool swept by the M5 wick — confirm price closed back "
    "inside, no round-number/session-extreme level.\n"
    "- structural_level: far edge used for SL + 0.5 ATR buffer.\n"
    "- rr: skip when below the configured 2R minimum.\n"
    "- session: London/NY only, no entries in the first 15 minutes of London.\n"
    "- Management: BE at 1R; exit on an opposite M5 BOS; one re-entry per level only.\n"
    "Flag any signal where these rules are violated or the structure is ambiguous."
)

PRE_TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["proceed", "abstain"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "market_conditions": {"type": "object"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "supporting_factors": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": [
        "decision",
        "confidence",
        "summary",
        "market_conditions",
        "risks",
        "supporting_factors",
        "notes",
    ],
}

POST_TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome_assessment": {"type": "string"},
        "factors": {"type": "array", "items": {"type": "string"}},
        "comparison": {"type": "object"},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "actionable_recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "outcome_assessment",
        "factors",
        "comparison",
        "lessons",
        "actionable_recommendations",
    ],
}

DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "market_summary": {"type": "string"},
        "performance_review": {"type": "object"},
        "patterns_identified": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "opportunities": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "description": {"type": "string"},
                    "proposed_config": {"type": "object"},
                },
                "required": ["hypothesis", "description", "proposed_config"],
            },
        },
    },
    "required": [
        "market_summary",
        "performance_review",
        "patterns_identified",
        "weaknesses",
        "opportunities",
        "recommendations",
    ],
}


def pre_trade_user_prompt(signal: dict, market_context: dict, account_context: dict) -> str:
    return (
        "Evaluate this candidate trade and return your assessment as JSON.\n\n"
        f"Signal: {signal}\n"
        f"Market context: {market_context}\n"
        f"Account context: {account_context}\n\n"
        f"{ICT_EVALUATION_GUIDANCE}\n\n"
        "Decision is 'proceed' only if the setup is genuinely favourable and the "
        "ICT structure is valid. Confidence 0.0-1.0. Identify concrete risks and "
        "supporting factors."
    )


def post_trade_user_prompt(trade: dict, market_context: dict, historical_context: dict) -> str:
    return (
        "Review this completed trade and return your analysis as JSON.\n\n"
        f"Trade: {trade}\n"
        f"Market context: {market_context}\n"
        f"Recent history / comparable trades: {historical_context}\n\n"
        "Explain WHY it won or lost, paying attention to the close reason "
        "('tp' take-profit, 'sl' stop-loss, 'bos' opposite break-of-structure exit, "
        "'breakeven' moved-to-BE scratch). Compare to historical performance and "
        "list actionable, testable recommendations."
    )


def daily_user_prompt(summary_stats: dict, recent_trades: list, news: list) -> str:
    return (
        "Analyze the last 24h of the trading system and return your analysis as JSON.\n\n"
        f"Performance summary: {summary_stats}\n"
        f"Recent trades: {recent_trades}\n"
        f"Recent news/sentiment: {news}\n\n"
        "Compare winning vs losing trades, identify recurring patterns and "
        "weaknesses, and propose improvements (each must be testable via backtest)."
    )
