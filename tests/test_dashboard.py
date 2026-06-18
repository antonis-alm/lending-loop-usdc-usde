import sys
import types

streamlit_stub = types.ModuleType("streamlit")
streamlit_stub.title = lambda *_args, **_kwargs: None
streamlit_stub.caption = lambda *_args, **_kwargs: None
streamlit_stub.subheader = lambda *_args, **_kwargs: None
streamlit_stub.columns = lambda n: [None] * n
streamlit_stub.metric = lambda *_args, **_kwargs: None
streamlit_stub.warning = lambda *_args, **_kwargs: None
streamlit_stub.success = lambda *_args, **_kwargs: None
streamlit_stub.info = lambda *_args, **_kwargs: None
streamlit_stub.error = lambda *_args, **_kwargs: None
streamlit_stub.dataframe = lambda *_args, **_kwargs: None
streamlit_stub.divider = lambda *_args, **_kwargs: None
sys.modules.setdefault("streamlit", streamlit_stub)

import dashboard.ui as ui


class _DummyColumn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_extract_position_rows_from_open_positions():
    state = {
        "open_positions": {
            "positions": [
                {
                    "position_type": "BORROW",
                    "protocol": "aave_v3",
                    "value_usd": "125.5",
                    "details": {"token": "USDE", "market_id": "aave_v3"},
                },
                {
                    "position_type": "SUPPLY",
                    "protocol": "aave_v3",
                    "value_usd": "220.0",
                    "details": {"token": "USDC", "market_id": "aave_v3"},
                },
            ]
        }
    }

    rows = ui._extract_position_rows(state)

    assert len(rows) == 2
    assert rows[0]["type"] == "BORROW"
    assert rows[0]["token"] == "USDE"
    assert rows[0]["value_usd"] == 125.5
    assert rows[1]["type"] == "SUPPLY"
    assert rows[1]["token"] == "USDC"


def test_render_custom_dashboard_tracks_current_positions(monkeypatch):
    strategy_config = {
        "collateral_token": "USDC",
        "borrow_token": "USDE",
        "max_loops": 3,
        "target_hf": "1.6",
        "stop_loop_hf": "1.45",
        "emergency_hf": "1.30",
    }
    session_state = {
        "phase": "loop_borrow",
        "loops_completed": 1,
        "loop_halted": False,
        "last_swap_rate": "0.9998",
        "position_health": {
            "health_factor": "1.58",
            "collateral_value_usd": "310.25",
            "debt_value_usd": "184.10",
            "lltv": "0.83",
        },
        "open_positions": {
            "positions": [
                {
                    "position_type": "BORROW",
                    "protocol": "aave_v3",
                    "value_usd": "184.10",
                    "details": {"token": "USDE", "market_id": "aave_v3"},
                }
            ]
        },
        "total_usdc_supplied": "310.25",
        "total_usde_borrowed": "184.10",
        "last_failure_reason": "",
    }

    metrics: list[tuple[str, str]] = []
    dataframe_rows: list[dict[str, object]] = []
    info_msgs: list[str] = []

    monkeypatch.setattr(ui.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "subheader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "columns", lambda n: [_DummyColumn() for _ in range(n)])
    monkeypatch.setattr(ui.st, "metric", lambda label, value, **_kwargs: metrics.append((label, value)))
    monkeypatch.setattr(ui.st, "dataframe", lambda rows, **_kwargs: dataframe_rows.extend(rows))
    monkeypatch.setattr(ui.st, "info", lambda msg, **_kwargs: info_msgs.append(msg))
    monkeypatch.setattr(ui.st, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "divider", lambda *_args, **_kwargs: None)

    ui.render_custom_dashboard("dep-123", strategy_config, api_client=object(), session_state=session_state)

    metric_map = dict(metrics)
    assert metric_map["Health Factor"] == "1.580"
    assert metric_map["Collateral Value (USD)"] == "310.25"
    assert metric_map["Debt Value (USD)"] == "184.10"
    assert metric_map["Total USDC Supplied"] == "310.250000"
    assert metric_map["Total USDE Borrowed"] == "184.100000"
    assert not info_msgs

    assert len(dataframe_rows) == 1
    assert dataframe_rows[0]["type"] == "BORROW"
    assert dataframe_rows[0]["token"] == "USDE"
    assert dataframe_rows[0]["value_usd"] == 184.1


def test_render_custom_dashboard_handles_missing_open_positions(monkeypatch):
    strategy_config = {"collateral_token": "USDC", "borrow_token": "USDE"}
    session_state = {
        "phase": "monitor",
        "loops_completed": 3,
        "loop_halted": True,
        "last_failure_reason": "swap reverted",
    }

    info_msgs: list[str] = []

    monkeypatch.setattr(ui.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "subheader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "columns", lambda n: [_DummyColumn() for _ in range(n)])
    monkeypatch.setattr(ui.st, "metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "dataframe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "info", lambda msg, **_kwargs: info_msgs.append(msg))
    monkeypatch.setattr(ui.st, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "divider", lambda *_args, **_kwargs: None)

    ui.render_custom_dashboard("dep-321", strategy_config, api_client=object(), session_state=session_state)

    assert info_msgs == ["No open position breakdown in session state yet."]
