"""Tests for AiSEG2 air environment parsing."""

from lxml import html

from custom_components.aiseg2_bridge import AiSeg2Client


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
