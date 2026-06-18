"""Custom dashboard for Lending Loop USDC/USDE strategy."""

from decimal import Decimal
from typing import Any

import streamlit as st


def _as_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _runtime_state(session_state: dict[str, Any] | None) -> dict[str, Any]:
    return dict(session_state or {})


def _position_health(state: dict[str, Any]) -> dict[str, Any]:
    health = _as_dict(state.get("position_health"))
    if health:
        return health
    keys = {"health_factor", "collateral_value_usd", "debt_value_usd", "lltv"}
    if keys.intersection(state.keys()):
        return state
    return {}


def _extract_position_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = state.get("open_positions", state.get("positions"))
    if isinstance(candidate, dict):
        candidate = candidate.get("positions")

    rows: list[dict[str, Any]] = []
    for item in _as_list(candidate):
        if not isinstance(item, dict):
            continue
        details = _as_dict(item.get("details"))
        rows.append(
            {
                "type": str(item.get("position_type", "unknown")),
                "protocol": str(item.get("protocol", "")),
                "token": str(details.get("token", item.get("token", ""))),
                "market": str(details.get("market_id", item.get("market_id", ""))),
                "value_usd": float(_as_decimal(item.get("value_usd"))),
            }
        )
    return rows


def _render_overview(state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    loops_completed = int(state.get("loops_completed", 0) or 0)
    max_loops = int(strategy_config.get("max_loops", 3) or 3)
    phase = str(state.get("phase", "monitor") or "monitor")
    halted = bool(state.get("loop_halted", False))
    halt_reason = str(state.get("halt_reason", "") or "").strip()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Loops", f"{loops_completed}/{max_loops}")
    with col2:
        st.metric("Phase", phase)
    with col3:
        st.metric("Loop Halted", "Yes" if halted else "No")
    with col4:
        st.metric("Last Swap Rate", f"{_as_decimal(state.get('last_swap_rate')):.6f}")

    if halted and halt_reason:
        st.warning(f"Loop halted reason: {halt_reason}")
    elif halted:
        st.warning("Loop is halted")
    else:
        st.success("Loop is active")


def _render_current_positions(state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    collateral_token = str(strategy_config.get("collateral_token", "USDC"))
    borrow_token = str(strategy_config.get("borrow_token", "USDE"))

    health = _position_health(state)
    health_factor = _as_decimal(health.get("health_factor", state.get("last_health_factor")))
    collateral_usd = _as_decimal(health.get("collateral_value_usd"))
    debt_usd = _as_decimal(health.get("debt_value_usd"))
    lltv = _as_decimal(health.get("lltv"))

    st.subheader("Current Positions")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Health Factor", f"{health_factor:.3f}")
    with col2:
        st.metric("Collateral Value (USD)", f"{collateral_usd:.2f}")
    with col3:
        st.metric("Debt Value (USD)", f"{debt_usd:.2f}")
    with col4:
        st.metric("LLTV", f"{lltv:.2%}" if lltv > 0 else "n/a")

    rows = _extract_position_rows(state)
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("No open position breakdown in session state yet.")

    st.metric(f"Total {collateral_token} Supplied", f"{_as_decimal(state.get('total_usdc_supplied')):.6f}")
    st.metric(f"Total {borrow_token} Borrowed", f"{_as_decimal(state.get('total_usde_borrowed')):.6f}")


def _render_risk_controls(strategy_config: dict[str, Any]) -> None:
    st.subheader("Risk Controls")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Target HF", f"{_as_decimal(strategy_config.get('target_hf', '1.6')):.2f}")
    with col2:
        st.metric("Stop HF", f"{_as_decimal(strategy_config.get('stop_loop_hf', '1.45')):.2f}")
    with col3:
        st.metric("Emergency HF", f"{_as_decimal(strategy_config.get('emergency_hf', '1.30')):.2f}")


def _render_recent_activity(state: dict[str, Any]) -> None:
    st.subheader("Recent Activity")
    failure = str(state.get("last_failure_reason", "") or "").strip()
    if failure:
        st.error(f"Last failure: {failure}")
    else:
        st.success("No recent execution failures")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    _ = api_client
    state = _runtime_state(session_state)

    st.title("Lending Loop USDC/USDE")
    st.caption(f"Deployment: {deployment_id}")

    _render_overview(state, strategy_config)
    st.divider()
    _render_current_positions(state, strategy_config)
    st.divider()
    _render_risk_controls(strategy_config)
    st.divider()
    _render_recent_activity(state)
