from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any

from almanak.framework.data.interfaces import DataSourceUnavailable
from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.market.errors import BalanceUnavailableError, HealthUnavailableError, PriceUnavailableError
from almanak.framework.strategies import IntentStrategy, almanak_strategy


class LoopPhase(StrEnum):
    BOOTSTRAP_SUPPLY = "bootstrap_supply"
    LOOP_BORROW = "loop_borrow"
    LOOP_SWAP = "loop_swap"
    LOOP_RESUPPLY = "loop_resupply"
    MONITOR = "monitor"


@dataclass
class RiskSnapshot:
    health_factor: Decimal
    collateral_usd: Decimal
    debt_usd: Decimal
    lltv: Decimal


@almanak_strategy(
    name="lending_loop_u_s_d_c_u_s_d_e",
    description="USDC collateral / USDE borrow finite 3-loop strategy on Aave V3",
    version="1.0.0",
    author="Almanak",
    tags=["lending", "aave_v3", "loop", "usdc", "usde"],
    supported_chains=["ethereum"],
    supported_protocols=["aave_v3"],
    intent_types=["SUPPLY", "BORROW", "SWAP", "REPAY", "WITHDRAW", "HOLD"],
    default_chain="ethereum",
    quote_asset="USD",
)
class LendingLoopUSDCUSDEStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def cfg(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.lending_protocol = str(cfg("lending_protocol", "aave_v3"))
        self.lending_market = str(cfg("lending_market", "aave_v3"))
        self.collateral_token = str(cfg("collateral_token", "USDC"))
        self.borrow_token = str(cfg("borrow_token", "USDE"))
        self.max_loops = int(cfg("max_loops", 3))
        self.initial_supply_pct = Decimal(str(cfg("initial_supply_pct", "1.0")))
        self.target_hf = Decimal(str(cfg("target_hf", "1.6")))
        self.min_hf_after_action = Decimal(str(cfg("min_hf_after_action", "1.5")))
        self.stop_loop_hf = Decimal(str(cfg("stop_loop_hf", "1.45")))
        self.emergency_hf = Decimal(str(cfg("emergency_hf", "1.30")))
        self.min_swap_rate = Decimal(str(cfg("min_swap_rate", "0.999")))
        self.max_swap_slippage = Decimal(str(cfg("max_swap_slippage", "0.001")))
        self.usde_depeg_floor = Decimal(str(cfg("usde_depeg_floor", "0.995")))
        self.usde_depeg_ceiling = Decimal(str(cfg("usde_depeg_ceiling", "1.005")))
        self.min_borrow_usd = Decimal(str(cfg("min_borrow_usd", "10")))
        self.interest_rate_mode = str(cfg("interest_rate_mode", "variable"))
        self.force_action = str(cfg("force_action", "")).strip().lower()

        if self.max_loops != 3:
            raise ValueError("max_loops must be exactly 3")
        if self.max_swap_slippage > Decimal("0.001"):
            raise ValueError("max_swap_slippage cannot exceed 0.001 (0.10%)")
        if self.stop_loop_hf <= self.emergency_hf:
            raise ValueError("stop_loop_hf must be greater than emergency_hf")

        self._phase = LoopPhase.BOOTSTRAP_SUPPLY
        self._loops_completed = 0
        self._loop_halted = False
        self._halt_reason = ""
        self._pending_supply_is_loop = False

        self.last_health_factor = Decimal("0")
        self.min_health_factor_seen = Decimal("999")
        self.total_usdc_supplied = Decimal("0")
        self.total_usde_borrowed = Decimal("0")
        self.total_usde_swapped = Decimal("0")
        self.total_usde_repaid = Decimal("0")
        self.last_swap_rate = Decimal("0")
        self.last_borrow_amount = Decimal("0")
        self.last_failure_reason = ""

    def decide(self, market: MarketSnapshot) -> Intent:
        if self.force_action:
            return self._forced_intent(market)

        risk = self._read_risk_snapshot(market)
        if isinstance(risk, Intent):
            return risk

        self.last_health_factor = risk.health_factor
        if risk.health_factor < self.min_health_factor_seen:
            self.min_health_factor_seen = risk.health_factor

        if risk.health_factor < self.emergency_hf:
            self._loop_halted = True
            self._phase = LoopPhase.MONITOR
            return self._emergency_repay_or_hold(market, risk, reason="hf_below_emergency")

        if risk.health_factor < self.stop_loop_hf:
            self._loop_halted = True
            self._phase = LoopPhase.MONITOR
            return Intent.hold(reason=f"hf {risk.health_factor} below stop_loop_hf {self.stop_loop_hf}")

        if self._loops_completed >= self.max_loops or self._loop_halted:
            self._phase = LoopPhase.MONITOR
            return self._monitor_intent(market, risk)

        if self._phase == LoopPhase.BOOTSTRAP_SUPPLY:
            return self._bootstrap_supply(market)
        if self._phase == LoopPhase.LOOP_BORROW:
            return self._borrow_for_loop(market, risk)
        if self._phase == LoopPhase.LOOP_SWAP:
            return self._swap_borrowed_to_collateral(market)
        if self._phase == LoopPhase.LOOP_RESUPPLY:
            return self._resupply_after_swap(market)

        self._phase = LoopPhase.MONITOR
        return self._monitor_intent(market, risk)

    def _bootstrap_supply(self, market: MarketSnapshot) -> Intent:
        balance = self._read_token_balance(market, self.collateral_token)
        if isinstance(balance, Intent):
            return balance
        if balance <= 0:
            return Intent.hold(reason=f"no {self.collateral_token} balance for bootstrap supply")

        amount = (balance * self.initial_supply_pct).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if amount <= 0:
            return Intent.hold(reason="bootstrap supply amount resolves to zero")

        self._pending_supply_is_loop = False
        self._phase = LoopPhase.LOOP_BORROW
        return Intent.supply(
            protocol=self.lending_protocol,
            token=self.collateral_token,
            amount=amount,
            use_as_collateral=True,
            chain=self.chain,
        )

    def _borrow_for_loop(self, market: MarketSnapshot, risk: RiskSnapshot) -> Intent:
        if risk.health_factor < self.min_hf_after_action:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason=f"current hf {risk.health_factor} below min_hf_after_action {self.min_hf_after_action}")

        usde_price = self._read_price(market, self.borrow_token)
        if isinstance(usde_price, Intent):
            return usde_price

        target_debt_usd = (risk.collateral_usd * risk.lltv) / self.target_hf
        borrow_delta_usd = target_debt_usd - risk.debt_usd
        if borrow_delta_usd < self.min_borrow_usd:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason=f"borrow delta usd {borrow_delta_usd} below min_borrow_usd {self.min_borrow_usd}")

        borrow_amount = (borrow_delta_usd / usde_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if borrow_amount <= 0:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason="computed borrow amount is zero")

        projected_debt_usd = risk.debt_usd + (borrow_amount * usde_price)
        projected_hf = (risk.collateral_usd * risk.lltv) / projected_debt_usd if projected_debt_usd > 0 else Decimal("0")
        if projected_hf < self.min_hf_after_action:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason=f"projected hf {projected_hf} below min_hf_after_action {self.min_hf_after_action}")

        self.last_borrow_amount = borrow_amount
        self._phase = LoopPhase.LOOP_SWAP
        return Intent.borrow(
            protocol=self.lending_protocol,
            collateral_token=self.collateral_token,
            collateral_amount=Decimal("0"),
            borrow_token=self.borrow_token,
            borrow_amount=borrow_amount,
            interest_rate_mode=self.interest_rate_mode,
            chain=self.chain,
        )

    def _swap_borrowed_to_collateral(self, market: MarketSnapshot) -> Intent:
        usde_price = self._read_price(market, self.borrow_token)
        if isinstance(usde_price, Intent):
            return usde_price

        usdc_price = self._read_price(market, self.collateral_token)
        if isinstance(usdc_price, Intent):
            return usdc_price

        if usde_price < self.usde_depeg_floor or usde_price > self.usde_depeg_ceiling:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            self._halt_reason = f"usde_depeg_{usde_price}"
            return Intent.hold(reason=f"USDE depeg guard tripped at {usde_price}")

        implied_rate = usde_price / usdc_price
        self.last_swap_rate = implied_rate
        if implied_rate < self.min_swap_rate:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            self._halt_reason = f"poor_rate_{implied_rate}"
            return Intent.hold(reason=f"implied swap rate {implied_rate} below {self.min_swap_rate}")

        if self.max_swap_slippage > Decimal("0.001"):
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            self._halt_reason = "slippage_config_exceeds_limit"
            return Intent.hold(reason="max_swap_slippage exceeds 0.10% policy")

        debt_balance = self._read_token_balance(market, self.borrow_token)
        if isinstance(debt_balance, Intent):
            return debt_balance
        if debt_balance <= 0:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason=f"no {self.borrow_token} available for swap")

        self._phase = LoopPhase.LOOP_RESUPPLY
        return Intent.swap(
            from_token=self.borrow_token,
            to_token=self.collateral_token,
            amount="all",
            max_slippage=self.max_swap_slippage,
            chain=self.chain,
        )

    def _resupply_after_swap(self, market: MarketSnapshot) -> Intent:
        usdc_balance = self._read_token_balance(market, self.collateral_token)
        if isinstance(usdc_balance, Intent):
            return usdc_balance
        if usdc_balance <= 0:
            self._phase = LoopPhase.MONITOR
            self._loop_halted = True
            return Intent.hold(reason=f"no {self.collateral_token} balance for re-supply")

        if self._loops_completed >= self.max_loops:
            self._phase = LoopPhase.MONITOR
            return Intent.hold(reason=f"loop cap reached: {self._loops_completed}")

        self._pending_supply_is_loop = True
        self._phase = LoopPhase.LOOP_BORROW
        return Intent.supply(
            protocol=self.lending_protocol,
            token=self.collateral_token,
            amount=usdc_balance,
            use_as_collateral=True,
            chain=self.chain,
        )

    def _monitor_intent(self, market: MarketSnapshot, risk: RiskSnapshot) -> Intent:
        if risk.health_factor < self.emergency_hf:
            return self._emergency_repay_or_hold(market, risk, reason="monitor_emergency")
        if risk.health_factor < self.stop_loop_hf:
            return Intent.hold(reason=f"monitor defensive: hf {risk.health_factor} below {self.stop_loop_hf}")
        return Intent.hold(reason=f"monitor only: loops_completed={self._loops_completed}, hf={risk.health_factor}")

    def _emergency_repay_or_hold(self, market: MarketSnapshot, risk: RiskSnapshot, reason: str) -> Intent:
        usde_balance = self._read_token_balance(market, self.borrow_token)
        if isinstance(usde_balance, Intent):
            return usde_balance

        usde_price = self._read_price(market, self.borrow_token)
        if isinstance(usde_price, Intent):
            return usde_price

        if usde_balance <= 0:
            return Intent.hold(reason=f"{reason}: emergency hf but no {self.borrow_token} to repay")

        debt_token_estimate = (risk.debt_usd / usde_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        repay_amount = min(usde_balance, debt_token_estimate)
        if repay_amount <= 0:
            return Intent.hold(reason=f"{reason}: repay amount resolved to zero")

        return Intent.repay(
            protocol=self.lending_protocol,
            token=self.borrow_token,
            amount=repay_amount,
            interest_rate_mode=self.interest_rate_mode,
            chain=self.chain,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        action = self.force_action
        if action == "supply":
            bal = self._read_token_balance(market, self.collateral_token)
            if isinstance(bal, Intent):
                return bal
            if bal <= 0:
                return Intent.hold(reason="force supply: no collateral balance")
            return Intent.supply(
                protocol=self.lending_protocol,
                token=self.collateral_token,
                amount=bal,
                use_as_collateral=True,
                chain=self.chain,
            )

        if action == "borrow":
            risk = self._read_risk_snapshot(market)
            if isinstance(risk, Intent):
                return risk
            return self._borrow_for_loop(market, risk)

        if action == "swap":
            return self._swap_borrowed_to_collateral(market)

        if action == "repay":
            risk = self._read_risk_snapshot(market)
            if isinstance(risk, Intent):
                return risk
            return self._emergency_repay_or_hold(market, risk, reason="force_repay")

        if action == "withdraw":
            return Intent.withdraw(
                protocol=self.lending_protocol,
                token=self.collateral_token,
                amount="all",
                withdraw_all=True,
                chain=self.chain,
            )

        return Intent.hold(reason=f"unknown force_action={action}")

    def _read_price(self, market: MarketSnapshot, token: str) -> Decimal | Intent:
        try:
            price = Decimal(str(market.price(token)))
        except (PriceUnavailableError, DataSourceUnavailable):
            return Intent.hold(reason=f"price unavailable for {token}")
        if price <= 0:
            return Intent.hold(reason=f"invalid non-positive price for {token}")
        return price

    def _read_token_balance(self, market: MarketSnapshot, token: str) -> Decimal | Intent:
        try:
            bal = market.balance(token)
        except (BalanceUnavailableError, DataSourceUnavailable):
            return Intent.hold(reason=f"balance unavailable for {token}")
        return Decimal(str(getattr(bal, "balance", bal)))

    def _read_risk_snapshot(self, market: MarketSnapshot) -> RiskSnapshot | Intent:
        try:
            pos = market.position_health(protocol=self.lending_protocol, market_id=self.lending_market)
        except (HealthUnavailableError, DataSourceUnavailable, ValueError):
            return Intent.hold(reason="position health unavailable")

        hf = Decimal(str(getattr(pos, "health_factor", "0")))
        collateral = Decimal(str(getattr(pos, "collateral_value_usd", "0")))
        debt = Decimal(str(getattr(pos, "debt_value_usd", "0")))
        lltv = Decimal(str(getattr(pos, "lltv", "0.83")))

        if hf <= 0:
            hf = Decimal("999")
        if lltv <= 0:
            lltv = Decimal("0.83")

        return RiskSnapshot(health_factor=hf, collateral_usd=collateral, debt_usd=debt, lltv=lltv)

    def on_intent_executed(self, intent: Intent, success: bool, result: Any) -> None:
        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")
        if success:
            if intent_type == "SUPPLY":
                amount = getattr(intent, "amount", None)
                if isinstance(amount, Decimal):
                    self.total_usdc_supplied += amount
                if self._pending_supply_is_loop and self._loops_completed < self.max_loops:
                    self._loops_completed += 1
                    self._pending_supply_is_loop = False
                    if self._loops_completed >= self.max_loops:
                        self._phase = LoopPhase.MONITOR
            elif intent_type == "BORROW":
                amount = getattr(intent, "borrow_amount", None)
                if isinstance(amount, Decimal):
                    self.total_usde_borrowed += amount
            elif intent_type == "SWAP":
                self.total_usde_swapped += self.last_borrow_amount
            elif intent_type == "REPAY":
                if bool(getattr(intent, "repay_full", False)):
                    self.total_usde_repaid = self.total_usde_borrowed
                else:
                    amount = getattr(intent, "amount", None)
                    if isinstance(amount, Decimal):
                        self.total_usde_repaid += amount
            self.last_failure_reason = ""
            return

        failure_text = self._failure_text(result)
        self.last_failure_reason = failure_text

        if intent_type == "SWAP":
            self._loop_halted = True
            self._phase = LoopPhase.MONITOR
            self._halt_reason = f"swap_failed:{failure_text}"
            return

        if intent_type in {"BORROW", "SUPPLY"} and any(
            needle in failure_text
            for needle in ["liquidity", "cap", "ceiling", "insufficient", "frozen", "paused", "borrow"]
        ):
            self._loop_halted = True
            self._phase = LoopPhase.MONITOR
            self._halt_reason = f"aave_constraint:{failure_text}"

    def _failure_text(self, result: Any) -> str:
        if result is None:
            return "unknown"
        parts: list[str] = []
        for key in ("error", "reason", "message"):
            value = getattr(result, key, None)
            if value:
                parts.append(str(value).lower())
        extracted = getattr(result, "extracted_data", None)
        if isinstance(extracted, dict):
            for key in ("error", "reason", "message"):
                value = extracted.get(key)
                if value:
                    parts.append(str(value).lower())
        return " | ".join(parts) if parts else "unknown"

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "lending_loop_u_s_d_c_u_s_d_e",
            "chain": self.chain,
            "phase": self._phase.value,
            "loops_completed": self._loops_completed,
            "loop_halted": self._loop_halted,
            "halt_reason": self._halt_reason,
            "last_health_factor": str(self.last_health_factor),
            "min_health_factor_seen": str(self.min_health_factor_seen),
            "total_usdc_supplied": str(self.total_usdc_supplied),
            "total_usde_borrowed": str(self.total_usde_borrowed),
            "total_usde_swapped": str(self.total_usde_swapped),
            "total_usde_repaid": str(self.total_usde_repaid),
            "last_swap_rate": str(self.last_swap_rate),
            "last_failure_reason": self.last_failure_reason,
        }

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "phase": self._phase.value,
            "loops_completed": self._loops_completed,
            "loop_halted": self._loop_halted,
            "halt_reason": self._halt_reason,
            "last_health_factor": str(self.last_health_factor),
            "min_health_factor_seen": str(self.min_health_factor_seen),
            "total_usdc_supplied": str(self.total_usdc_supplied),
            "total_usde_borrowed": str(self.total_usde_borrowed),
            "total_usde_swapped": str(self.total_usde_swapped),
            "total_usde_repaid": str(self.total_usde_repaid),
            "last_swap_rate": str(self.last_swap_rate),
            "last_borrow_amount": str(self.last_borrow_amount),
            "last_failure_reason": self.last_failure_reason,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self._phase = LoopPhase(state.get("phase", LoopPhase.BOOTSTRAP_SUPPLY.value))
        self._loops_completed = int(state.get("loops_completed", 0))
        self._loop_halted = bool(state.get("loop_halted", False))
        self._halt_reason = str(state.get("halt_reason", ""))
        self.last_health_factor = Decimal(str(state.get("last_health_factor", "0")))
        self.min_health_factor_seen = Decimal(str(state.get("min_health_factor_seen", "999")))
        self.total_usdc_supplied = Decimal(str(state.get("total_usdc_supplied", "0")))
        self.total_usde_borrowed = Decimal(str(state.get("total_usde_borrowed", "0")))
        self.total_usde_swapped = Decimal(str(state.get("total_usde_swapped", "0")))
        self.total_usde_repaid = Decimal(str(state.get("total_usde_repaid", "0")))
        self.last_swap_rate = Decimal(str(state.get("last_swap_rate", "0")))
        self.last_borrow_amount = Decimal(str(state.get("last_borrow_amount", "0")))
        self.last_failure_reason = str(state.get("last_failure_reason", ""))

    def supports_teardown(self) -> bool:
        return True

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions: list[PositionInfo] = []
        snapshot = self.create_market_snapshot()
        risk = self._read_risk_snapshot(snapshot)
        if isinstance(risk, RiskSnapshot):
            if risk.debt_usd > 0:
                positions.append(
                    PositionInfo(
                        position_type=PositionType.BORROW,
                        position_id="aave_usde_debt",
                        chain=self.chain,
                        protocol=self.lending_protocol,
                        value_usd=risk.debt_usd,
                        details={"token": self.borrow_token, "market_id": self.lending_market},
                    )
                )
            if risk.collateral_usd > 0:
                positions.append(
                    PositionInfo(
                        position_type=PositionType.SUPPLY,
                        position_id="aave_usdc_supply",
                        chain=self.chain,
                        protocol=self.lending_protocol,
                        value_usd=risk.collateral_usd,
                        details={"token": self.collateral_token, "market_id": self.lending_market},
                    )
                )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "lending_loop_u_s_d_c_u_s_d_e"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode, market=None) -> list[Intent]:
        snapshot = market if market is not None else self.create_market_snapshot()
        risk = self._read_risk_snapshot(snapshot)
        if isinstance(risk, Intent):
            return []

        intents: list[Intent] = []
        max_slippage = Decimal("0.003") if getattr(mode, "value", "") == "hard" else Decimal("0.001")

        usde_balance = self._read_token_balance(snapshot, self.borrow_token)
        if isinstance(usde_balance, Intent):
            usde_balance = Decimal("0")

        if risk.debt_usd > 0:
            usde_price = self._read_price(snapshot, self.borrow_token)
            if isinstance(usde_price, Intent):
                return []
            debt_tokens = (risk.debt_usd / usde_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            if usde_balance < debt_tokens and risk.collateral_usd > 0:
                shortfall = debt_tokens - usde_balance
                intents.append(
                    Intent.withdraw(
                        protocol=self.lending_protocol,
                        token=self.collateral_token,
                        amount=shortfall,
                        chain=self.chain,
                    )
                )
                intents.append(
                    Intent.swap(
                        from_token=self.collateral_token,
                        to_token=self.borrow_token,
                        amount="all",
                        max_slippage=max_slippage,
                        chain=self.chain,
                    )
                )
            intents.append(
                Intent.repay(
                    protocol=self.lending_protocol,
                    token=self.borrow_token,
                    repay_full=True,
                    interest_rate_mode=self.interest_rate_mode,
                    chain=self.chain,
                )
            )

        if risk.collateral_usd > 0:
            intents.append(
                Intent.withdraw(
                    protocol=self.lending_protocol,
                    token=self.collateral_token,
                    amount="all",
                    withdraw_all=True,
                    chain=self.chain,
                )
            )

        return intents
