# LendingLoopUSDCUSDEStrategy - Agent Guide

> AI coding agent context for the `lending_loop_u_s_d_c_u_s_d_e` strategy.

## Overview

- **Template:** lending_loop
- **Chain:** ethereum
- **Class:** `LendingLoopUSDCUSDEStrategy` in `strategy.py`
- **Config:** `config.json`

Dependencies are declared in `pyproject.toml`.

## Files

| File | Purpose |
|------|---------|
| `strategy.py` | Main strategy - edit `decide()` to change trading logic |
| `config.json` | Runtime parameters (tokens, thresholds, chain) |
| `pyproject.toml` | Dependencies plus metadata (`framework`, `version`, `run.interval`) |
| `.env` | Secrets (private key, API keys) - never commit this |
| `.gitignore` | Git ignore rules (excludes `.venv/`, `.env`, etc.) |
| `.python-version` | Python version pin (3.12) |
| `tests/test_strategy.py` | Unit tests for the strategy |

## How to Run

```bash
# Single iteration on Anvil fork (safe, no real funds)
almanak strat run --network anvil --once

# Single iteration on mainnet
almanak strat run --once

# Continuous with 30s interval
almanak strat run --network anvil --interval 30

# Dry run (no transactions)
almanak strat run --dry-run --once
```

## Adding Dependencies

Edit the `dependencies` list in `pyproject.toml`.

## Config Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `collateral_token` | string | Token to supply as collateral (e.g. WETH) |
| `borrow_token` | string | Token to borrow (e.g. USDC) |
| `supply_amount` | string (Decimal) | Initial collateral amount to supply |
| `borrow_amount` | string (Decimal) | First-loop borrow amount in borrow_token |
| `target_leverage` | string (Decimal) | Target leverage (e.g. 2.0 = 2x) |
| `borrow_ratio` | string (Decimal) | LTV usage per loop (0.7 = 70%), controls borrow decay. Must be < 1.0; values >= 1.0 cause exponential borrow growth. |
| `min_health_factor` | string (Decimal) | Minimum health factor before repay (e.g. 1.5) |
| `min_collateral_usd` | string (Decimal) | Minimum collateral USD to start |


All values in `config.json` are read via `self.config.get("key", default)` in `__init__`.
String-typed Decimals (e.g. `"0.005"`) are used to avoid floating-point precision issues.

## Intent Types Used

This strategy uses these intent types:

- `Intent.supply(protocol, token, amount, use_as_collateral=True)`
- `Intent.borrow(protocol, collateral_token, collateral_amount, borrow_token, borrow_amount)`
- `Intent.swap(from_token, to_token, amount_usd=, max_slippage=Decimal("0.005"))`
- `Intent.repay(protocol, token, amount, repay_full=False)`
- `Intent.withdraw(protocol, token, amount, withdraw_all=False)`
- `Intent.hold(reason="...")`

All intents are created via `from almanak.framework.intents import Intent`.

## Key Patterns

- `decide(market)` receives a `MarketSnapshot` with `market.price()`, `market.balance()`, `market.rsi()`, etc.
- Return an `Intent` object or `Intent.hold(reason=...)` from `decide()`
- Always wrap `decide()` logic in try/except, returning `Intent.hold()` on error
- Config values are read via `self.config.get("key", default)` in `__init__`
- State persists between iterations via `self.state` dict

## Common Mistakes

- borrow_ratio MUST be between 0 and 1 (e.g. 0.7 = 70% LTV per loop). Values >= 1.0 cause exponential borrow growth.
- Each loop borrows borrow_amount * borrow_ratio^loop_count -- amounts decay geometrically.
## Teardown (Required)

Every `IntentStrategy` **must** implement two abstract teardown methods.
Strategies that hold no positions can extend `StatelessStrategy` instead.

| Method | Purpose |
|--------|---------|
| `get_open_positions() -> TeardownPositionSummary` | List positions to close (query on-chain state, not cache) |
| `generate_teardown_intents(mode, market) -> list[Intent]` | Return ordered intents to unwind positions |

**Execution order** (if multiple position types): PERP -> BORROW -> SUPPLY -> LP -> TOKEN

The generated `strategy.py` includes teardown stubs with TODO comments -- fill them in.
See `docs/internal/blueprints/14-teardown-system.md` for the full teardown system reference.

## Testing

```bash
# Unit tests
pytest tests/ -v

# Lifecycle + teardown on a managed Anvil fork
# (drives each force_action through the production code path, then runs teardown)
almanak strat test --actions <csv> --teardown --json

# Paper trade (Anvil fork with PnL tracking)
almanak strat backtest paper --duration 3600 --interval 60

# PnL backtest (historical prices)
almanak strat backtest pnl --start 2024-01-01 --end 2024-06-01
```

## Full SDK Reference

For the complete intent vocabulary, market data API, and advanced patterns,
install the full agent skill:

```bash
almanak agent install
```

Or read the bundled skill directly:

```bash
almanak docs agent-skill --dump
```
