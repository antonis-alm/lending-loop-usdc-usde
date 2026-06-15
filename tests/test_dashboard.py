import sys
import types
from types import SimpleNamespace

streamlit_stub = types.ModuleType("streamlit")
streamlit_stub.title = lambda *_args, **_kwargs: None
streamlit_stub.subheader = lambda *_args, **_kwargs: None
streamlit_stub.columns = lambda n: [None] * n
streamlit_stub.metric = lambda *_args, **_kwargs: None
streamlit_stub.warning = lambda *_args, **_kwargs: None
streamlit_stub.success = lambda *_args, **_kwargs: None
streamlit_stub.divider = lambda *_args, **_kwargs: None
sys.modules.setdefault("streamlit", streamlit_stub)

templates_stub = types.ModuleType("almanak.framework.dashboard.templates")
templates_stub.get_aave_v3_config = lambda **kwargs: SimpleNamespace(**kwargs)
templates_stub.prepare_lending_session_state = lambda api_client, **kwargs: kwargs.get("session_state", {})
templates_stub.render_lending_dashboard = lambda *_args, **_kwargs: None
sys.modules.setdefault("almanak.framework.dashboard.templates", templates_stub)

import dashboard.ui as ui


class _DummyColumn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_render_custom_dashboard_uses_lending_template(monkeypatch):
    strategy_config = {
        "collateral_token": "USDC",
        "borrow_token": "USDE",
        "chain": "ethereum",
        "max_loops": 3,
    }
    session_state = {"phase": "loop_borrow", "loops_completed": 1}
    fake_config = SimpleNamespace(protocol="aave_v3")

    captured: dict[str, object] = {}

    def fake_get_aave_v3_config(**kwargs):
        captured["config_kwargs"] = kwargs
        return fake_config

    def fake_prepare_lending_session_state(api_client, session_state, config, strategy_config):
        captured["prepared"] = {
            "phase": "loop_swap",
            "loops_completed": 2,
            "loop_halted": False,
        }
        return captured["prepared"]

    def fake_render_metrics(state, cfg):
        captured["metrics_state"] = state
        captured["metrics_cfg"] = cfg

    def fake_render_lending_dashboard(deployment_id, cfg, state, config):
        captured["dashboard_call"] = {
            "deployment_id": deployment_id,
            "cfg": cfg,
            "state": state,
            "config": config,
        }

    monkeypatch.setattr(ui, "get_aave_v3_config", fake_get_aave_v3_config)
    monkeypatch.setattr(ui, "prepare_lending_session_state", fake_prepare_lending_session_state)
    monkeypatch.setattr(ui, "_render_strategy_metrics", fake_render_metrics)
    monkeypatch.setattr(ui, "render_lending_dashboard", fake_render_lending_dashboard)
    monkeypatch.setattr(ui.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "divider", lambda *_args, **_kwargs: None)

    ui.render_custom_dashboard("dep-123", strategy_config, api_client=object(), session_state=session_state)

    assert captured["config_kwargs"] == {
        "collateral_token": "USDC",
        "borrow_token": "USDE",
        "chain": "ethereum",
    }
    assert captured["metrics_state"] == captured["prepared"]
    assert captured["metrics_cfg"] == strategy_config
    assert captured["dashboard_call"] == {
        "deployment_id": "dep-123",
        "cfg": strategy_config,
        "state": captured["prepared"],
        "config": fake_config,
    }


def test_render_custom_dashboard_falls_back_when_prepare_fails(monkeypatch):
    strategy_config = {"collateral_token": "USDC", "borrow_token": "USDE", "chain": "ethereum"}
    session_state = {"phase": "monitor", "loops_completed": 3}

    captured: dict[str, object] = {}

    monkeypatch.setattr(ui, "get_aave_v3_config", lambda **_kwargs: SimpleNamespace(protocol="aave_v3"))
    monkeypatch.setattr(ui, "prepare_lending_session_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ui, "_render_strategy_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "divider", lambda *_args, **_kwargs: None)

    def fake_render_lending_dashboard(_deployment_id, _cfg, state, _config):
        captured["state"] = state

    monkeypatch.setattr(ui, "render_lending_dashboard", fake_render_lending_dashboard)

    ui.render_custom_dashboard("dep-321", strategy_config, api_client=object(), session_state=session_state)

    assert captured["state"] == session_state


def test_render_strategy_metrics_shows_required_metrics(monkeypatch):
    metrics: list[tuple[str, str]] = []
    warnings: list[str] = []

    monkeypatch.setattr(ui.st, "subheader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui.st, "columns", lambda n: [_DummyColumn() for _ in range(n)])
    monkeypatch.setattr(ui.st, "metric", lambda label, value, **_kwargs: metrics.append((label, value)))
    monkeypatch.setattr(ui.st, "warning", lambda msg, **_kwargs: warnings.append(msg))
    monkeypatch.setattr(ui.st, "success", lambda *_args, **_kwargs: None)

    state = {
        "loops_completed": 2,
        "phase": "loop_swap",
        "loop_halted": True,
        "halt_reason": "poor_rate_0.998",
        "last_health_factor": "1.46",
        "total_usdc_supplied": "2500.5",
        "total_usde_borrowed": "1499.8",
        "last_swap_rate": "0.9995",
        "last_failure_reason": "swap reverted",
    }
    strategy_config = {"max_loops": 3, "stop_loop_hf": "1.45", "emergency_hf": "1.30"}

    ui._render_strategy_metrics(state, strategy_config)

    labels = {label for label, _value in metrics}
    assert "Loops Completed" in labels
    assert "Phase" in labels
    assert "Loop Halted" in labels
    assert "Recent Action" in labels
    assert "Health Factor (Current)" in labels
    assert "HF Stop/Emergency" in labels
    assert "Collateral Supplied (USDC)" in labels
    assert "Debt Borrowed (USDE)" in labels
    assert "Last Swap Rate (USDE→USDC)" in labels

    metric_map = dict(metrics)
    assert metric_map["Loops Completed"] == "2/3"
    assert metric_map["Phase"] == "loop_swap"
    assert metric_map["Loop Halted"] == "Yes"
    assert metric_map["Recent Action"] == "failed: swap reverted"
    assert warnings == ["Loop halted reason: poor_rate_0.998"]
