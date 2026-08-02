"""AiSEG2 to MQTT Integration for Home Assistant."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from lxml import html

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Regex for number extraction
_NUM = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
_AIR_DEVICE_INFO = re.compile(r"var\s+deviceInfo\s*=\s*(\[.*?\]);", re.DOTALL)
_AIR_DEVICE_COUNT = re.compile(r"var\s+deviceCnt\s*=\s*(\d+)")


def _to_float(s: Optional[str]) -> float:
    """Convert string to float, handling Japanese characters."""
    if not s:
        return 0.0
    try:
        t = s.replace("，", ",").replace("．", ".").replace(",", "")
        m = _NUM.search(t)
        value = float(m.group(1)) if m else 0.0
        return _validate_energy_value(value, s)
    except (ValueError, TypeError) as err:
        _LOGGER.warning("Failed to parse energy value '%s': %s", s, err)
        return 0.0


def _validate_energy_value(value: float, original_str: str) -> float:
    """Validate energy values are reasonable."""
    if value < 0:
        _LOGGER.warning("Negative energy value detected: %f (from '%s')", value, original_str)
        return 0.0
    if value > 999999:  # Suspiciously large value (>999MWh per day)
        _LOGGER.warning("Suspiciously large energy value: %f (from '%s')", value, original_str)
        return 0.0
    return value


@dataclass
class AiSeg2Config:
    """Configuration for AiSEG2 client."""

    host: str
    user: str = "aiseg"
    password: str = ""
    timeout: float = 10.0


class AiSeg2Client:
    """Async client for AiSEG2 pages (Digest auth)."""

    def __init__(self, cfg: AiSeg2Config):
        """Initialize the client."""
        self._cfg = cfg
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self):
        """Ensure the httpx client is initialized."""
        if self._client is None:
            import asyncio

            def create_client():
                return httpx.AsyncClient(
                    base_url=f"http://{self._cfg.host}",
                    timeout=self._cfg.timeout,
                    auth=httpx.DigestAuth(self._cfg.user, self._cfg.password),
                    headers={"User-Agent": "aiseg2/ha-integration"},
                )

            # Run SSL initialization in a separate thread to avoid blocking the event loop
            self._client = await asyncio.to_thread(create_client)

    async def close(self):
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_html_texts(self, path: str, xpath: str) -> List[str]:
        """Fetch HTML and extract text using XPath."""
        root = await self._get_html(path)
        return [t for t in root.xpath(xpath) if isinstance(t, str)]

    async def _get_html(self, path: str):
        """Fetch and parse an AiSEG2 HTML page."""
        await self._ensure_client()
        try:
            r = await self._client.get(path)
            r.raise_for_status()
            return html.fromstring(r.content)
        except httpx.TimeoutException:
            _LOGGER.warning("Timeout accessing %s on %s", path, self._cfg.host)
            raise

    async def _post_json(self, path: str) -> dict[str, Any]:
        """Fetch a JSON payload from an AiSEG2 read-only update endpoint."""
        await self._ensure_client()
        try:
            response = await self._client.post(path)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            _LOGGER.warning("Timeout accessing %s on %s", path, self._cfg.host)
            raise
        except httpx.ConnectError:
            _LOGGER.error("Connection failed to %s for path %s", self._cfg.host, path)
            raise
        except httpx.HTTPStatusError as err:
            _LOGGER.error("HTTP %d error from %s%s", err.response.status_code, self._cfg.host, path)
            raise
        except httpx.ConnectError:
            _LOGGER.error("Connection failed to %s for path %s", self._cfg.host, path)
            raise
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 401:
                _LOGGER.error("Authentication failed for %s (check credentials)", self._cfg.host)
            else:
                _LOGGER.error("HTTP %d error from %s%s", err.response.status_code, self._cfg.host, path)
            raise

    @staticmethod
    def _parse_display_number(card, class_name: str) -> Optional[float]:
        """Parse an AiSEG2 digit-display element into a numeric value."""
        displays = card.xpath(
            f'.//div[contains(concat(" ", normalize-space(@class), " "), " {class_name} ")]'
        )
        if not displays:
            return None

        display = displays[0]
        if "hidden" in (display.get("style") or ""):
            return None

        value = ""
        for digit in display.xpath("./div"):
            classes = digit.get("class") or ""
            if "num_dot" in classes:
                value += "."
            elif "num" in classes:
                if "no_minus" in classes:
                    value += "-"
                else:
                    match = re.search(r"(?:^|\s)no([0-9])(?:\s|$)", classes)
                    if match:
                        value += match.group(1)

        try:
            return float(value) if value and value not in {"-", ".", "-."} else None
        except ValueError:
            _LOGGER.warning("Failed to parse air environment value '%s'", value)
            return None

    @classmethod
    def _parse_air_environment_page(cls, root) -> tuple[int, list[dict[str, Any]]]:
        """Extract room metadata and temperature/humidity values from one page."""
        scripts = root.xpath("//script/text()")
        script_text = "\n".join(text for text in scripts if isinstance(text, str))
        count_match = _AIR_DEVICE_COUNT.search(script_text)
        info_match = _AIR_DEVICE_INFO.search(script_text)
        if not count_match or not info_match:
            return 0, []

        try:
            device_info = json.loads(info_match.group(1))
        except json.JSONDecodeError:
            _LOGGER.warning("Unable to parse air environment device metadata")
            return 0, []

        cards = root.xpath(
            '//div[contains(concat(" ", normalize-space(@class), " "), " base ")]'
            '[.//div[contains(concat(" ", normalize-space(@class), " "), " txt_place ")]]'
        )
        readings: list[dict[str, Any]] = []
        for metadata, card in zip(device_info, cards):
            devices = metadata.get("array", [])
            node_id = next((str(item["nodeId"]) for item in devices if "nodeId" in item), None)
            if not node_id:
                continue
            room_names = card.xpath(
                './/div[contains(concat(" ", normalize-space(@class), " "), " txt_place ")]/text()'
            )
            readings.append(
                {
                    "id": node_id,
                    "name": "".join(room_names).strip() or str(metadata.get("name") or node_id),
                    "temperature": cls._parse_display_number(card, "num_ond"),
                    "humidity": cls._parse_display_number(card, "num_shitudo"),
                }
            )
        return int(count_match.group(1)), readings

    async def fetch_temperature_humidity(self) -> dict[str, dict[str, Any]]:
        """Fetch temperature and humidity sensors configured in AiSEG2."""
        first_page = await self._get_html("/page/airenvironment/42?page=1")
        device_count, readings = self._parse_air_environment_page(first_page)
        readings_by_id = {reading["id"]: reading for reading in readings}

        for page in range(2, (device_count + 2) // 3 + 1):
            root = await self._get_html(f"/page/airenvironment/42?page={page}")
            _, page_readings = self._parse_air_environment_page(root)
            readings_by_id.update({reading["id"]: reading for reading in page_readings})
        return readings_by_id

    @staticmethod
    def _parse_bath_auto(root) -> Optional[bool]:
        """Extract the automatic bath state from the EcoCute device panel."""
        panels = root.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " panel ")]')
        for panel in panels:
            title = "".join(panel.xpath('.//div[contains(@class, "kiki_title")]//text()')).strip()
            if "エコキュート" not in title:
                continue
            states = panel.xpath(
                './/div[contains(concat(" ", normalize-space(@class), " "), " state ") '
                'and contains(concat(" ", normalize-space(@class), " "), " bath_disp_on ")]'
            )
            if not states:
                return None
            classes = set((states[0].get("class") or "").split())
            if "on" in classes:
                return True
            if "off" in classes:
                return False
            return None
        return None

    async def fetch_bath_auto(self) -> Optional[bool]:
        """Fetch the current automatic bath state, if an EcoCute is configured."""
        return self._parse_bath_auto(await self._get_html("/page/devices/device/32"))

    @staticmethod
    def _parse_graph_value(root) -> Optional[float]:
        """Extract the primary numeric value from an AiSEG2 graph page."""
        values = root.xpath('//span[@id="val_kwh"]/text()')
        return _to_float(values[0]) if values else None

    async def fetch_water_usage(self) -> Optional[float]:
        """Fetch today's water usage in cubic metres."""
        return self._parse_graph_value(await self._get_html("/page/graph/56111"))

    @staticmethod
    def _parse_current_power(payload: dict[str, Any]) -> Optional[float]:
        """Extract whole-home current power in watts from the update response."""
        if payload.get("measReg") != 1:
            return None
        value = payload.get("u_capacity")
        if value in (None, "", "-"):
            return None
        try:
            return float(value) * 1000
        except (TypeError, ValueError):
            _LOGGER.warning("Failed to parse current power value '%s'", value)
            return None

    async def fetch_current_power(self) -> Optional[float]:
        """Fetch whole-home current power in watts."""
        return self._parse_current_power(await self._post_json("/data/electricflow/111/update"))

    @staticmethod
    def _normalize_circuit_name(name: str) -> str:
        """Normalize a display name for matching against the circuit catalog."""
        return re.sub(r"\s+", "", name).replace("・", "")

    @classmethod
    def _parse_circuit_power_page(cls, root) -> list[dict[str, Any]]:
        """Extract named circuit current-power readings from one circuit page."""
        readings: list[dict[str, Any]] = []
        for stage in root.xpath('//div[starts-with(@id, "stage_")]'):
            names = stage.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " c_device ")]//text()')
            values = stage.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " c_value ")]/text()')
            name = "".join(names).strip()
            value = "".join(values).strip()
            if not name or not value:
                continue
            match = _NUM.search(value)
            if not match:
                _LOGGER.warning("Failed to parse circuit power value '%s' for '%s'", value, name)
                continue
            readings.append({"name": name, "power_w": float(match.group(1))})
        return readings

    async def fetch_circuit_power(self) -> list[dict[str, Any]]:
        """Fetch current power readings for all circuit display pages."""
        readings: list[dict[str, Any]] = []
        for page in range(1, 4):
            root = await self._get_html(f"/page/electricflow/1113?id={page}")
            readings.extend(self._parse_circuit_power_page(root))
        return readings

    async def fetch_totals(self) -> Dict[str, float]:
        """Fetch today's total energy values (kWh)."""
        return {
            "total_use_kwh": _to_float(
                (await self._get_html_texts("/page/graph/52111", '//span[@id="val_kwh"]/text()'))[:1][0]
                if (await self._get_html_texts("/page/graph/52111", '//span[@id="val_kwh"]/text()'))
                else None
            ),
            "buy_kwh": _to_float(
                (await self._get_html_texts("/page/graph/53111", '//span[@id="val_kwh"]/text()'))[:1][0]
                if (await self._get_html_texts("/page/graph/53111", '//span[@id="val_kwh"]/text()'))
                else None
            ),
            "sell_kwh": _to_float(
                (await self._get_html_texts("/page/graph/54111", '//span[@id="val_kwh"]/text()'))[:1][0]
                if (await self._get_html_texts("/page/graph/54111", '//span[@id="val_kwh"]/text()'))
                else None
            ),
            "gen_kwh": _to_float(
                (await self._get_html_texts("/page/graph/51111", '//span[@id="val_kwh"]/text()'))[:1][0]
                if (await self._get_html_texts("/page/graph/51111", '//span[@id="val_kwh"]/text()'))
                else None
            ),
        }

    async def fetch_circuit_catalog(self) -> List[Dict[str, str]]:
        """Fetch list of available circuits."""
        await self._ensure_client()
        r = await self._client.get("/page/setting/installation/734")
        r.raise_for_status()
        root = html.fromstring(r.content)
        scripts = root.xpath('//script[contains(text(), "window.onload")]')
        if not scripts:
            return []
        text = scripts[0].text or ""
        l, rpos = text.find("("), text.rfind(")")
        if l < 0 or rpos <= l:
            return []
        data = json.loads(text[l + 1 : rpos].strip())
        out: List[Dict[str, str]] = []
        for c in data.get("arrayCircuitNameList", []):
            if c.get("strBtnType") == "1":
                cid = str(c.get("strId"))
                name = str(c.get("strCircuit") or f"Circuit {cid}")
                out.append({"id": cid, "name": name})
        return out

    async def fetch_circuit_kwh(self, circuit_id: str) -> float:
        """Fetch energy consumption for a specific circuit."""
        await self._ensure_client()
        params = {"circuitid": str(circuit_id)}
        b64 = base64.b64encode(json.dumps(params).encode()).decode()
        r = await self._client.get(f"/page/graph/584?data={b64}")
        r.raise_for_status()
        root = html.fromstring(r.content)
        vals = root.xpath('//span[@id="val_kwh"]/text()')
        return _to_float(vals[0] if vals else None)


class AiSeg2DataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from AiSEG2."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AiSeg2Client,
        scan_interval: int,
    ) -> None:
        """Initialize the data update coordinator."""
        self.client = client
        self.circuits: List[Dict[str, str]] = []

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _fetch_with_retry(self, fetch_func, description: str, max_retries: int = 3, retry_delay: float = 2.0):
        """Fetch data with retry logic."""
        for attempt in range(max_retries):
            try:
                result = await fetch_func()
                if attempt > 0:
                    _LOGGER.info("Successfully %s after %d retries", description, attempt)
                return result
            except Exception as err:
                if attempt == max_retries - 1:
                    _LOGGER.error("Failed to %s after %d attempts: %s", description, max_retries, err)
                    raise
                _LOGGER.warning(
                    "Attempt %d/%d failed for %s, retrying in %.1fs: %s",
                    attempt + 1,
                    max_retries,
                    description,
                    retry_delay,
                    err,
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AiSEG2."""
        _LOGGER.debug("Starting data fetch from AiSEG2 %s", self.client._cfg.host)

        try:
            # Fetch circuits if not already cached
            if not self.circuits:
                _LOGGER.debug("Fetching circuit catalog from %s", self.client._cfg.host)
                self.circuits = await self._fetch_with_retry(self.client.fetch_circuit_catalog, "fetch circuit catalog")
                _LOGGER.info("Found %d circuits on %s", len(self.circuits), self.client._cfg.host)
                for circuit in self.circuits:
                    _LOGGER.debug("Circuit %s: %s", circuit["id"], circuit["name"])

            # Fetch total energy values
            _LOGGER.debug("Fetching total energy values")
            totals = await self._fetch_with_retry(self.client.fetch_totals, "fetch total energy values")
            _LOGGER.debug("Total energy values: %s", totals)

            try:
                environment = await self._fetch_with_retry(
                    self.client.fetch_temperature_humidity,
                    "fetch temperature and humidity",
                    max_retries=2,
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch temperature and humidity: %s", err)
                environment = {}

            try:
                bath_auto = await self._fetch_with_retry(
                    self.client.fetch_bath_auto,
                    "fetch bath auto state",
                    max_retries=2,
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch bath auto state: %s", err)
                bath_auto = None

            try:
                water_usage_m3 = await self._fetch_with_retry(
                    self.client.fetch_water_usage,
                    "fetch water usage",
                    max_retries=2,
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch water usage: %s", err)
                water_usage_m3 = None

            try:
                current_power_w = await self._fetch_with_retry(
                    self.client.fetch_current_power,
                    "fetch current power",
                    max_retries=2,
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch current power: %s", err)
                current_power_w = None

            try:
                raw_circuit_power = await self._fetch_with_retry(
                    self.client.fetch_circuit_power,
                    "fetch circuit current power",
                    max_retries=2,
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch circuit current power: %s", err)
                raw_circuit_power = []

            catalog_by_name: dict[str, list[str]] = {}
            for circuit in self.circuits:
                catalog_by_name.setdefault(self.client._normalize_circuit_name(circuit["name"]), []).append(circuit["id"])
            circuit_power: dict[str, float] = {}
            for reading in raw_circuit_power:
                matching_ids = catalog_by_name.get(self.client._normalize_circuit_name(reading["name"]), [])
                if len(matching_ids) == 1:
                    circuit_power[matching_ids[0]] = reading["power_w"]
                elif matching_ids:
                    _LOGGER.warning("Skipping ambiguous circuit power reading for '%s'", reading["name"])
                else:
                    _LOGGER.debug("No circuit catalog match for current power reading '%s'", reading["name"])

            # Fetch per-circuit values
            circuit_data = {}
            for circuit in self.circuits:
                circuit_id = circuit["id"]
                try:
                    _LOGGER.debug("Fetching energy for circuit %s (%s)", circuit_id, circuit["name"])
                    kwh_value = await self._fetch_with_retry(
                        lambda: self.client.fetch_circuit_kwh(circuit_id),
                        f"fetch circuit {circuit_id} energy",
                        max_retries=2,  # Fewer retries for individual circuits
                    )
                    circuit_data[circuit_id] = {
                        "name": circuit["name"],
                        "kwh": kwh_value,
                    }
                    _LOGGER.debug("Circuit %s (%s): %.3f kWh", circuit_id, circuit["name"], kwh_value)
                except Exception as err:
                    _LOGGER.warning("Failed to fetch data for circuit %s (%s): %s", circuit_id, circuit["name"], err)
                    # Skip this circuit instead of sending 0.0 (which could be a real value)
                    continue

            # Calculate last reset (today at midnight JST)
            jst = timezone(timedelta(hours=9))
            now = datetime.now(jst)
            last_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)

            result = {
                "totals": totals,
                "circuits": circuit_data,
                "environment": environment,
                "bath_auto": bath_auto,
                "water_usage_m3": water_usage_m3,
                "current_power_w": current_power_w,
                "circuit_power": circuit_power,
                "last_reset": last_reset.isoformat(),
                "timestamp": now.isoformat(),
            }

            _LOGGER.info(
                "Successfully fetched data from AiSEG2 %s: %d total metrics, %d circuits",
                self.client._cfg.host,
                len(totals),
                len(circuit_data),
            )
            return result

        except httpx.TimeoutException:
            _LOGGER.error("Timeout connecting to AiSEG2 %s", self.client._cfg.host)
            raise UpdateFailed("Connection timeout")
        except httpx.ConnectError:
            _LOGGER.error("Cannot connect to AiSEG2 %s (check network/IP address)", self.client._cfg.host)
            raise UpdateFailed("Connection failed")
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 401:
                _LOGGER.error("Authentication failed for AiSEG2 %s (check username/password)", self.client._cfg.host)
                raise UpdateFailed("Authentication failed")
            else:
                _LOGGER.error("HTTP error %d from AiSEG2 %s", err.response.status_code, self.client._cfg.host)
                raise UpdateFailed(f"HTTP error {err.response.status_code}")
        except Exception as err:
            _LOGGER.error("Unexpected error communicating with AiSEG2 %s: %s", self.client._cfg.host, err)
            raise UpdateFailed(f"Unexpected error: {err}") from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AiSEG2 from a config entry."""
    config = AiSeg2Config(
        host=entry.data["host"],
        user=entry.data["username"],
        password=entry.data["password"],
    )

    client = AiSeg2Client(config)

    # Test the connection
    _LOGGER.info("Testing connection to AiSEG2 at %s", config.host)
    try:
        await client.fetch_totals()
        _LOGGER.info("Successfully connected to AiSEG2 at %s", config.host)
    except httpx.TimeoutException:
        await client.close()
        _LOGGER.error("Connection timeout to AiSEG2 %s during setup", config.host)
        raise ConfigEntryNotReady("Connection timeout - check network connectivity")
    except httpx.ConnectError:
        await client.close()
        _LOGGER.error("Cannot connect to AiSEG2 %s during setup", config.host)
        raise ConfigEntryNotReady("Cannot connect - check IP address and network")
    except httpx.HTTPStatusError as err:
        await client.close()
        if err.response.status_code == 401:
            _LOGGER.error("Authentication failed for AiSEG2 %s during setup", config.host)
            raise ConfigEntryNotReady("Authentication failed - check username and password")
        else:
            _LOGGER.error("HTTP error %d from AiSEG2 %s during setup", err.response.status_code, config.host)
            raise ConfigEntryNotReady(f"HTTP error {err.response.status_code}")
    except Exception as ex:
        await client.close()
        _LOGGER.error("Unexpected error connecting to AiSEG2 %s: %s", config.host, ex)
        raise ConfigEntryNotReady(f"Cannot connect to AiSEG2: {ex}") from ex

    # Create the data update coordinator
    coordinator = AiSeg2DataUpdateCoordinator(
        hass,
        client,
        entry.options.get("scan_interval", 300),
    )

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Get coordinator
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Close the client
        await coordinator.client.close()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
