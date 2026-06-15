"""Lending Loop USDC/USDE dashboard."""

from decimal import Decimal
from typing import Any

import streamlit as st

from almanak.framework.dashboard.templates import (
    get_aave_v3_config,
    prepare_lending_session_state,
    render_lending_dashboard,
)


def _as_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _recent_action_state(state: dict[str, Any]) -> str:
    last_failure_reason = str(state.get("last_failure_reason", "")).strip()
    if last_failure_reason:
        return f"failed: {last_failure_reason}"

    explicit = str(state.get("recent_action_state", "") or state.get("last_action_state", "")).strip()
    if explicit:
        return explicit

    phase = str(state.get("phase", "monitor")).strip() or "monitor"
    return f"active: {phase}"


def _render_strategy_metrics(state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    loops_completed = int(state.get("loops_completed", 0) or 0)
    max_loops = int(strategy_config.get("max_loops", 3) or 3)
    phase = str(state.get("phase", "monitor"))
    loop_halted = bool(state.get("loop_halted", False))
    halt_reason = str(state.get("halt_reason", "")).strip()

    current_hf = _as_decimal(state.get("last_health_factor"))
    stop_hf = _as_decimal(strategy_config.get("stop_loop_hf", "1.45"), Decimal("1.45"))
    emergency_hf = _as_decimal(strategy_config.get("emergency_hf", "1.30"), Decimal("1.30"))

    collateral_total = _as_decimal(state.get("total_usdc_supplied"))
    debt_total = _as_decimal(state.get("total_usde_borrowed"))
    swap_rate = _as_decimal(state.get("last_swap_rate"))
    recent_action = _recent_action_state(state)

    st.subheader("Loop Strategy Metrics")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Loops Completed", f"{loops_completed}/{max_loops}")
    with col2:
        st.metric("Phase", phase)
    with col3:
        st.metric("Loop Halted", "Yes" if loop_halted else "No")
    with col4:
        st.metric("Recent Action", recent_action)

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        st.metric("Health Factor (Current)", f"{current_hf:.3f}")
    with col6:
        st.metric("HF Stop/Emergency", f"{stop_hf:.2f} / {emergency_hf:.2f}")
    with col7:
        st.metric("Collateral Supplied (USDC)", f"{collateral_total:.6f}")
    with col8:
        st.metric("Debt Borrowed (USDE)", f"{debt_total:.6f}")

    st.metric("Last Swap Rate (USDE→USDC)", f"{swap_rate:.6f}")

    if loop_halted and halt_reason:
        st.warning(f"Loop halted reason: {halt_reason}")
    elif loop_halted:
        st.warning("Loop is halted")
    else:
        st.success("Loop is active")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    config = get_aave_v3_config(
        collateral_token=str(strategy_config.get("collateral_token", "USDC")),
        borrow_token=str(strategy_config.get("borrow_token", "USDE")),
        chain=str(strategy_config.get("chain", "ethereum")),
    )

    try:
        hydrated_state = prepare_lending_session_state(
            api_client,
            session_state=session_state,
            config=config,
            strategy_config=strategy_config,
        )
    except Exception:
        hydrated_state = dict(session_state or {})

    st.title("Lending-Loop-USDC-USDE")
    _render_strategy_metrics(hydrated_state, strategy_config)
    st.divider()
    render_lending_dashboard(deployment_id, strategy_config, hydrated_state, config)
