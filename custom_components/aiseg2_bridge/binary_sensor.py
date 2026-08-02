"""Binary sensors for AiSEG2 Bridge."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up AiSEG2 binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.data and coordinator.data.get("bath_auto") is not None:
        async_add_entities([BathAutoBinarySensor(coordinator, entry, entry.data["host"])])


class BathAutoBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Represent the EcoCute automatic bath operation state."""

    _attr_device_class = "running"
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, host: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._host = host
        self._attr_name = "Bath Auto"
        self._attr_unique_id = f"{DOMAIN}-{host}-bath-auto"
        self.entity_id = "binary_sensor.aiseg2_bridge_bath_auto"

    @property
    def device_info(self):
        """Return the AiSEG2 device this entity belongs to."""
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}-{self._host}")},
            "name": f"AiSEG2 ({self._host})",
            "manufacturer": "Panasonic",
            "model": "AiSEG2",
        }

    @property
    def is_on(self):
        """Return whether automatic bath operation is enabled."""
        return self.coordinator.data.get("bath_auto")
