from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

JST = timezone(timedelta(hours=9))

TOTAL_KEYS = [
    ("total_use_kwh", "Total Energy Today（電力量）"),
    ("buy_kwh", "Purchased Energy Today（電力量）"),
    ("sell_kwh", "Sold Energy Today（電力量）"),
    ("gen_kwh", "Generated Energy Today（電力量）"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    host = entry.data["host"]

    ents = []
    # 合計系
    for key, name in TOTAL_KEYS:
        ents.append(TotalEnergySensor(coordinator, entry, host, key, name))
    if coordinator.data and coordinator.data.get("current_power_w") is not None:
        ents.append(CurrentPowerSensor(coordinator, entry, host))

    # 回路系
    if coordinator.data:
        circuits = coordinator.data.get("circuits", {})
        for cid, cdata in circuits.items():
            ents.append(CircuitEnergySensor(coordinator, entry, host, cid, cdata["name"]))
            # AiSEG2 omits c_value for inactive circuits. Create the entity
            # regardless so it becomes available when the circuit next draws power.
            ents.append(CircuitPowerSensor(coordinator, entry, host, cid, cdata["name"]))
        for sensor_id, sensor_data in coordinator.data.get("environment", {}).items():
            ents.append(TemperatureSensor(coordinator, entry, host, sensor_id, sensor_data["name"]))
            ents.append(HumiditySensor(coordinator, entry, host, sensor_id, sensor_data["name"]))
        if coordinator.data.get("water_usage_m3") is not None:
            ents.append(WaterUsageSensor(coordinator, entry, host))

    async_add_entities(ents)


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, host: str):
        super().__init__(coordinator)
        self._host = host
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}-{self._host}")},
            "name": f"AiSEG2 ({self._host})",
            "manufacturer": "Panasonic",
            "model": "AiSEG2",
        }

class _DailyTotalSensor(_Base):
    """Base class for values that reset at midnight JST."""

    _attr_state_class = "total"

    @property
    def last_reset(self) -> datetime:
        """Return the start of the current AiSEG2 reporting day."""
        now = datetime.now(JST)
        return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=JST)


class TotalEnergySensor(_DailyTotalSensor):
    _attr_device_class = "energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry: ConfigEntry, host: str, key: str, disp_name: str):
        super().__init__(coordinator, entry, host)
        self._key = key
        self._attr_name = disp_name
        self._attr_unique_id = f"{DOMAIN}-{host}-{key}"
        # Set explicit entity_id (use underscore format of key)
        self.entity_id = f"sensor.aiseg2_bridge_{key.replace('_kwh', '')}"

    @property
    def native_value(self) -> Optional[float]:
        totals = self.coordinator.data.get("totals", {})
        v = totals.get(self._key)
        return float(v) if v is not None else None


class CircuitEnergySensor(_DailyTotalSensor):
    _attr_device_class = "energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry: ConfigEntry, host: str, cid: str, cname: str):
        super().__init__(coordinator, entry, host)
        self._cid = str(cid)
        self._cname = cname
        self._attr_name = f"{cname}（電力量）"
        self._attr_unique_id = f"{DOMAIN}-{host}-c{self._cid}"
        # Set explicit entity_id
        self.entity_id = f"sensor.aiseg2_bridge_c{self._cid}"

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        circuits = self.coordinator.data.get("circuits", {})
        circuit_data = circuits.get(self._cid)
        if circuit_data:
            return float(circuit_data.get("kwh", 0))
        return None


class TemperatureSensor(_Base):
    _attr_device_class = "temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = "measurement"

    def __init__(self, coordinator, entry: ConfigEntry, host: str, sensor_id: str, room_name: str):
        super().__init__(coordinator, entry, host)
        self._sensor_id = sensor_id
        self._attr_name = f"{room_name}（温度）"
        self._attr_unique_id = f"{DOMAIN}-{host}-environment-{sensor_id}-temperature"
        self.entity_id = f"sensor.aiseg2_bridge_environment_{sensor_id}_temperature"

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data.get("environment", {}).get(self._sensor_id, {})
        return data.get("temperature")


class HumiditySensor(_Base):
    _attr_device_class = "humidity"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = "measurement"

    def __init__(self, coordinator, entry: ConfigEntry, host: str, sensor_id: str, room_name: str):
        super().__init__(coordinator, entry, host)
        self._sensor_id = sensor_id
        self._attr_name = f"{room_name}（湿度）"
        self._attr_unique_id = f"{DOMAIN}-{host}-environment-{sensor_id}-humidity"
        self.entity_id = f"sensor.aiseg2_bridge_environment_{sensor_id}_humidity"

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data.get("environment", {}).get(self._sensor_id, {})
        return data.get("humidity")


class WaterUsageSensor(_DailyTotalSensor):
    _attr_device_class = "water"
    _attr_native_unit_of_measurement = "m³"
    _attr_state_class = "total"

    def __init__(self, coordinator, entry: ConfigEntry, host: str):
        super().__init__(coordinator, entry, host)
        self._attr_name = "Water Usage Today"
        self._attr_unique_id = f"{DOMAIN}-{host}-water-usage-m3"
        self.entity_id = "sensor.aiseg2_bridge_water_usage_today"

    @property
    def native_value(self) -> Optional[float]:
        return self.coordinator.data.get("water_usage_m3")


class CurrentPowerSensor(_Base):
    _attr_device_class = "power"
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = "measurement"

    def __init__(self, coordinator, entry: ConfigEntry, host: str):
        super().__init__(coordinator, entry, host)
        self._attr_name = "Current Power Usage（電力）"
        self._attr_unique_id = f"{DOMAIN}-{host}-current-power"
        self.entity_id = "sensor.aiseg2_bridge_current_power_usage"

    @property
    def native_value(self) -> Optional[float]:
        return self.coordinator.data.get("current_power_w")


class CircuitPowerSensor(_Base):
    _attr_device_class = "power"
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = "measurement"

    def __init__(self, coordinator, entry: ConfigEntry, host: str, circuit_id: str, circuit_name: str):
        super().__init__(coordinator, entry, host)
        self._circuit_id = str(circuit_id)
        self._attr_name = f"{circuit_name}（電力）"
        self._attr_unique_id = f"{DOMAIN}-{host}-c{self._circuit_id}-power"
        self.entity_id = f"sensor.aiseg2_bridge_c{self._circuit_id}_power"

    @property
    def native_value(self) -> Optional[float]:
        return self.coordinator.data.get("circuit_power", {}).get(self._circuit_id)
