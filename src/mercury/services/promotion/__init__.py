"""Strategy promotion workflow (backtest → paper → demo → review → approved → live)."""

from mercury.services.promotion.service import PromotionError, PromotionService

__all__ = ["PromotionError", "PromotionService"]
