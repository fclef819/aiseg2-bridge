"""Tests for AiSEG2 sensor parsing and setup."""

import asyncio
from types import SimpleNamespace

from lxml import html

from custom_components.aiseg2_bridge import AiSeg2Client
from custom_components.aiseg2_bridge.sensor import (
    CircuitPowerSensor,
    HumiditySensor,
    TemperatureSensor,
    WaterUsageSensor,
    async_setup_entry,
)


def test_parse_air_environment_page() -> None:
    root = html.fromstring(
        """
        <script>
        var deviceCnt = 1;
        var deviceInfo = [{"name":"リビング","array":[{"nodeId":"123"}]}];
        </script>
        <div class="base">
          <div class="txt_place">リビング</div>
          <div class="num_ond"><div class="num no2"></div><div class="num no5"></div>
            <div class="num_dot"></div><div class="num no3"></div></div>
          <div class="num_shitudo"><div class="num no6"></div><div class="num no8"></div></div>
        </div>
        """
    )

    count, readings = AiSeg2Client._parse_air_environment_page(root)

    assert count == 1
    assert readings == [{"id": "123", "name": "リビング", "temperature": 25.3, "humidity": 68.0}]


def test_parse_air_environment_page_handles_hidden_values() -> None:
    root = html.fromstring(
        """
        <script>
        var deviceCnt = 1;
        var deviceInfo = [{"name":"寝室","array":[{"nodeId":"456"}]}];
        </script>
        <div class="base">
          <div class="txt_place">寝室</div>
          <div class="num_ond" style="visibility:hidden"></div>
          <div class="num_shitudo"><div class="num no4"></div><div class="num no0"></div></div>
        </div>
        """
    )

    _, readings = AiSeg2Client._parse_air_environment_page(root)

    assert readings[0]["temperature"] is None
    assert readings[0]["humidity"] == 40.0


def test_parse_bath_auto_state() -> None:
    root = html.fromstring(
        """
        <div class="panel">
          <div class="kiki_title">エコキュート</div>
          <div class="kiki_state"><div class="state off"></div>
            <div class="state on bath_disp_on">自動中</div></div>
        </div>
        """
    )

    assert AiSeg2Client._parse_bath_auto(root) is True


def test_parse_bath_auto_returns_none_without_ecocute() -> None:
    root = html.fromstring('<div class="panel"><div class="kiki_title">シャッター</div></div>')

    assert AiSeg2Client._parse_bath_auto(root) is None


def test_parse_delivery_box_state() -> None:
    empty_root = html.fromstring(
        '<div class="panel"><div class="kiki_title">宅配ボックス</div>'
        '<div class="kiki_val"><span>荷物なし</span></div></div>'
    )
    occupied_root = html.fromstring(
        '<div class="panel"><div class="kiki_title">宅配ボックス</div>'
        '<div class="kiki_val"><span>荷物あり</span></div></div>'
    )

    assert AiSeg2Client._parse_delivery_box(empty_root) is False
    assert AiSeg2Client._parse_delivery_box(occupied_root) is True


def test_parse_delivery_box_returns_none_when_not_configured() -> None:
    root = html.fromstring('<div class="panel"><div class="kiki_title">シャッター</div></div>')

    assert AiSeg2Client._parse_delivery_box(root) is None


def test_parse_water_usage() -> None:
    root = html.fromstring('<span id="val_kwh">0.110</span>')

    assert AiSeg2Client._parse_graph_value(root) == 0.11


def test_parse_water_usage_returns_none_when_not_configured() -> None:
    root = html.fromstring("<html></html>")

    assert AiSeg2Client._parse_graph_value(root) is None


def test_parse_current_power_converts_kw_to_w() -> None:
    assert AiSeg2Client._parse_current_power({"measReg": 1, "u_capacity": "2.8"}) == 2800.0
    assert AiSeg2Client._parse_current_power({"measReg": 0, "u_capacity": "2.8"}) is None


def test_parse_circuit_power_page() -> None:
    root = html.fromstring(
        """
        <div id="stage_1"><div class="c_value"> 1859W</div>
          <div class="c_device">キッチン<br />食器洗乾燥機</div></div>
        <div id="stage_2"><div class="c_value"></div><div class="c_device">未使用</div></div>
        """
    )

    assert AiSeg2Client._parse_circuit_power_page(root) == [
        {"name": "キッチン食器洗乾燥機", "power_w": 1859.0},
        {"name": "未使用", "power_w": 0.0},
    ]


def test_last_reset_is_only_provided_by_daily_total_sensors() -> None:
    assert "last_reset" not in TemperatureSensor.__dict__
    assert "last_reset" not in HumiditySensor.__dict__
    assert "last_reset" not in CircuitPowerSensor.__dict__
    assert "last_reset" in WaterUsageSensor.__mro__[1].__dict__


def test_setup_adds_power_entities_for_inactive_circuits() -> None:
    class Coordinator:
        data = {
            "totals": {},
            "circuits": {
                "1": {"name": "稼働中回路", "kwh": 1.0},
                "2": {"name": "停止中回路", "kwh": 0.0},
            },
            "circuit_power": {"1": 100.0},
        }

        def async_add_listener(self, _listener, _context=None):
            return lambda: None

    entry = SimpleNamespace(entry_id="entry", data={"host": "aiseg.local"})
    hass = SimpleNamespace(data={"aiseg2_bridge": {"entry": Coordinator()}})
    entities = []

    asyncio.run(async_setup_entry(hass, entry, entities.extend))

    assert {entity._circuit_id for entity in entities if isinstance(entity, CircuitPowerSensor)} == {"1", "2"}
