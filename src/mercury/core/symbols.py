"""Symbol mapping layer.

Strategies, signals, and the database reference canonical instrument ids
(e.g. ``GOLD``). Brokers speak their own symbols (e.g. ``XAUUSD``). The
:class:`SymbolMapper` translates between the two using the active
environment's symbol map and cross-checks availability against the broker.

Contract metadata (contract size, point, digits, min/max lot) lives here so
risk sizing and PnL accounting can stay symbol-aware.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from mercury.core.config import EnvironmentConfig
from mercury.core.logging import get_logger

logger = get_logger("core.symbols")


class SymbolMappingError(ValueError):
    """Raised when a canonical id or broker symbol cannot be mapped."""


@dataclass(frozen=True)
class InstrumentSpec:
    canonical: str
    broker_symbol: str
    preferred: bool
    contract_size: float
    point: float
    digits: int
    min_lot: float
    lot_step: float


class SymbolMapper:
    """Resolves canonical instrument ids to broker symbols and back."""

    def __init__(self, environment: EnvironmentConfig) -> None:
        self._canonical_to_broker: dict[str, str] = {}
        self._specs: dict[str, InstrumentSpec] = {}
        self._broker_to_canonical: dict[str, str] = {}
        for canonical, contract in environment.symbols.items():
            self._canonical_to_broker[canonical] = contract.broker_symbol
            self._broker_to_canonical[contract.broker_symbol] = canonical
            self._specs[canonical] = InstrumentSpec(
                canonical=canonical,
                broker_symbol=contract.broker_symbol,
                preferred=contract.preferred,
                contract_size=contract.contract_size,
                point=contract.point,
                digits=contract.digits,
                min_lot=contract.min_lot,
                lot_step=contract.lot_step,
            )

    def broker_symbol(self, canonical: str) -> str:
        try:
            return self._canonical_to_broker[canonical]
        except KeyError:
            raise SymbolMappingError(
                f"no broker symbol mapped for canonical instrument '{canonical}'"
            ) from None

    def canonical(self, broker_symbol: str) -> str:
        mapped = self._broker_to_canonical.get(broker_symbol)
        if mapped is not None:
            return mapped
        if broker_symbol in self._canonical_to_broker:
            return broker_symbol  # pass-through when a canonical id is used directly
        raise SymbolMappingError(f"no canonical instrument mapped for broker symbol '{broker_symbol}'")

    def spec(self, canonical: str) -> InstrumentSpec:
        spec = self._specs.get(canonical)
        if spec is None:
            raise SymbolMappingError(f"no contract metadata for canonical instrument '{canonical}'")
        return spec

    def contract_size(self, canonical: str) -> float:
        return self.spec(canonical).contract_size

    def canonical_ids(self) -> list[str]:
        return list(self._canonical_to_broker)

    def verify_available(self, available_symbols: Iterable[str]) -> list[str]:
        """Cross-check the environment symbol map against broker symbols.

        Logs a warning for each preferred symbol missing from the broker and
        for ambiguity (other broker symbols that look like a configured one).
        Never silently picks an alternative. Returns the canonical ids whose
        preferred symbol is present.
        """
        available = set(available_symbols)
        verified: list[str] = []
        for canonical in self._canonical_to_broker:
            spec = self._specs[canonical]
            if spec.broker_symbol not in available:
                logger.warning(
                    "preferred broker symbol not found",
                    extra={"canonical": canonical, "broker_symbol": spec.broker_symbol},
                )
                continue
            verified.append(canonical)
            extras = [
                s
                for s in available
                if s != spec.broker_symbol
                and (spec.broker_symbol.lower() in s.lower() or s.lower() in spec.broker_symbol.lower())
            ]
            if extras:
                logger.warning(
                    "ambiguous broker symbols for canonical instrument",
                    extra={"canonical": canonical, "preferred": spec.broker_symbol, "also_found": sorted(extras)},
                )
        return verified


def get_symbol_mapper(settings) -> SymbolMapper:
    """Build the mapper for the active environment."""
    return SymbolMapper(settings.environment)
