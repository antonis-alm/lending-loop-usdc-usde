from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy import LendingLoopUSDCUSDEStrategy, LoopPhase


def _cfg(**overrides):
    base = {
        "chain": "ethereum",
        "lending_protocol": "aave_v3",
        "lending_market": "aave_v3",
        "collateral_token": "USDC",
        "borrow_token": "USDE",
        "max_loops": 3,
        "initial_supply_pct": "1.0",
        "target_hf": "1.6",
        "min_hf_after_action": "1.5",
        "stop_loop_hf": "1.45",
        "emergency_hf": "1.30",
        "min_swap_rate": "0.999",
        "max_swap_slippage": "0.001",
        "usde_depeg_floor": "0.995",
        "usde_depeg_ceiling": "1.005",
        "min_borrow_usd": "10",
        "interest_rate_mode": "variable",
        "force_action": "",
    }
    base.update(overrides)
    return base


def _mk_market(
    *,
    hf=Decimal("1.7"),
    collateral_usd=Decimal("10000"),
    debt_usd=Decimal("0"),
    lltv=Decimal("0.83"),
    prices=None,
    balances=None,
):
    prices = prices or {"USDC": Decimal("1.0"), "USDE": Decimal("1.0")}
    balances = balances or {"USDC": Decimal("1000"), "USDE": Decimal("0")}

    class Market:
        def position_health(self, protocol, market_id):
            return SimpleNamespace(
                health_factor=hf,
                collateral_value_usd=collateral_usd,
                debt_value_usd=debt_usd,
                lltv=lltv,
            )

        def price(self, token):
            return prices[token]

        def balance(self, token):
            return SimpleNamespace(balance=balances.get(token, Decimal("0")))

    return Market()


@pytest.fixture
def strategy():
    return LendingLoopUSDCUSDEStrategy(config=_cfg(), chain="ethereum", wallet_address="0x" + "1" * 40)


def test_bootstrap_supply_uses_all_usdc(strategy):
    market = _mk_market()
    intent = strategy.decide(market)
    assert intent.intent_type.value == "SUPPLY"
    assert intent.token == "USDC"
    assert intent.amount == Decimal("1000")


def test_no_bootstrap_balance_holds(strategy):
    market = _mk_market(balances={"USDC": Decimal("0"), "USDE": Decimal("0")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_borrow_phase_emits_borrow_with_hf_projection(strategy):
    strategy._phase = LoopPhase.LOOP_BORROW
    market = _mk_market(debt_usd=Decimal("1000"), collateral_usd=Decimal("10000"), hf=Decimal("1.8"))
    intent = strategy.decide(market)
    assert intent.intent_type.value == "BORROW"
    assert intent.borrow_token == "USDE"
    assert intent.borrow_amount > Decimal("0")


def test_refuse_borrow_when_projected_hf_below_min():
    custom = LendingLoopUSDCUSDEStrategy(
        config=_cfg(target_hf="1.4", min_hf_after_action="1.5"),
        chain="ethereum",
        wallet_address="0x" + "1" * 40,
    )
    custom._phase = LoopPhase.LOOP_BORROW
    market = _mk_market(collateral_usd=Decimal("10000"), debt_usd=Decimal("3000"), hf=Decimal("1.55"), lltv=Decimal("0.83"))
    intent = custom.decide(market)
    assert intent.intent_type.value == "HOLD"
    assert "projected hf" in intent.reason


def test_swap_rate_guard_halts_loop(strategy):
    strategy._phase = LoopPhase.LOOP_SWAP
    strategy.last_borrow_amount = Decimal("100")
    market = _mk_market(prices={"USDC": Decimal("1.0"), "USDE": Decimal("0.998")}, balances={"USDC": Decimal("0"), "USDE": Decimal("100")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"
    assert strategy._loop_halted is True


def test_swap_depeg_guard_halts_loop(strategy):
    strategy._phase = LoopPhase.LOOP_SWAP
    market = _mk_market(prices={"USDC": Decimal("1.0"), "USDE": Decimal("1.02")}, balances={"USDC": Decimal("0"), "USDE": Decimal("100")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"
    assert "depeg" in intent.reason


def test_swap_success_transitions_to_resupply(strategy):
    strategy._phase = LoopPhase.LOOP_SWAP
    strategy.last_borrow_amount = Decimal("100")
    market = _mk_market(prices={"USDC": Decimal("1.0"), "USDE": Decimal("1.0")}, balances={"USDC": Decimal("0"), "USDE": Decimal("100")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "SWAP"


def test_resupply_uses_all_usdc(strategy):
    strategy._phase = LoopPhase.LOOP_RESUPPLY
    market = _mk_market(balances={"USDC": Decimal("350"), "USDE": Decimal("0")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "SUPPLY"
    assert intent.amount == Decimal("350")


def test_exactly_three_loops_then_monitor_only(strategy):
    market = _mk_market(balances={"USDC": Decimal("100"), "USDE": Decimal("0")})

    for _ in range(3):
        strategy._phase = LoopPhase.LOOP_RESUPPLY
        intent = strategy.decide(market)
        assert intent.intent_type.value == "SUPPLY"
        strategy.on_intent_executed(intent, True, SimpleNamespace())

    assert strategy._loops_completed == 3
    monitor_intent = strategy.decide(_mk_market())
    assert monitor_intent.intent_type.value == "HOLD"
    assert strategy._phase == LoopPhase.MONITOR


def test_emergency_repay_when_hf_below_threshold(strategy):
    market = _mk_market(hf=Decimal("1.25"), debt_usd=Decimal("500"), balances={"USDC": Decimal("0"), "USDE": Decimal("300")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "REPAY"
    assert intent.token == "USDE"


def test_emergency_hold_if_no_usde_to_repay(strategy):
    market = _mk_market(hf=Decimal("1.25"), debt_usd=Decimal("500"), balances={"USDC": Decimal("0"), "USDE": Decimal("0")})
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_failed_swap_halts(strategy):
    strategy.on_intent_executed(
        SimpleNamespace(intent_type=SimpleNamespace(value="SWAP")),
        False,
        SimpleNamespace(error="swap reverted"),
    )
    assert strategy._loop_halted is True
    assert strategy._phase == LoopPhase.MONITOR


def test_liquidity_failure_on_borrow_halts(strategy):
    strategy.on_intent_executed(
        SimpleNamespace(intent_type=SimpleNamespace(value="BORROW")),
        False,
        SimpleNamespace(message="insufficient liquidity in reserve"),
    )
    assert strategy._loop_halted is True
    assert "aave_constraint" in strategy._halt_reason


def test_force_action_borrow(strategy):
    strategy.force_action = "borrow"
    strategy._phase = LoopPhase.LOOP_BORROW
    market = _mk_market(debt_usd=Decimal("1000"), collateral_usd=Decimal("10000"), hf=Decimal("1.8"))
    intent = strategy.decide(market)
    assert intent.intent_type.value == "BORROW"


def test_persistent_state_round_trip(strategy):
    strategy._phase = LoopPhase.MONITOR
    strategy._loops_completed = 3
    strategy._loop_halted = True
    strategy.total_usdc_supplied = Decimal("123")
    dumped = strategy.get_persistent_state()

    fresh = LendingLoopUSDCUSDEStrategy(config=_cfg(), chain="ethereum", wallet_address="0x" + "2" * 40)
    fresh.load_persistent_state(dumped)

    assert fresh._phase == LoopPhase.MONITOR
    assert fresh._loops_completed == 3
    assert fresh._loop_halted is True
    assert fresh.total_usdc_supplied == Decimal("123")


def test_teardown_contains_repay_before_withdraw(strategy):
    class TeardownMarket:
        def position_health(self, protocol, market_id):
            return SimpleNamespace(
                health_factor=Decimal("1.6"),
                collateral_value_usd=Decimal("5000"),
                debt_value_usd=Decimal("1000"),
                lltv=Decimal("0.83"),
            )

        def price(self, token):
            return Decimal("1")

        def balance(self, token):
            if token == "USDE":
                return SimpleNamespace(balance=Decimal("0"))
            return SimpleNamespace(balance=Decimal("100"))

    class Mode:
        value = "soft"

    intents = strategy.generate_teardown_intents(Mode(), market=TeardownMarket())
    types = [i.intent_type.value for i in intents]
    assert "REPAY" in types
    assert "WITHDRAW" in types
    assert types.index("REPAY") < max(i for i, t in enumerate(types) if t == "WITHDRAW")
