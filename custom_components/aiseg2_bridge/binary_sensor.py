"""Binary sensors for AiSEG2 Bridge."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up AiSEG2 binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.data:
        return

    host = entry.data["host"]
    entities = []
    if coordinator.data.get("bath_auto") is not None:
        entities.append(BathAutoBinarySensor(coordinator, entry, host))
    if coordinator.data.get("delivery_box_occupied") is not None:
        entities.append(DeliveryBoxBinarySensor(coordinator, entry, host))
    async_add_entities(entities)


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


class DeliveryBoxBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Represent whether the delivery box contains a package."""

    _attr_device_class = "occupancy"
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, host: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._host = host
        self._attr_name = "宅配ボックス（着荷）"
        self._attr_unique_id = f"{DOMAIN}-{host}-delivery-box-occupied"
        self.entity_id = "binary_sensor.aiseg2_bridge_delivery_box_occupied"

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
        """Return whether a package is present."""
        return self.coordinator.data.get("delivery_box_occupied")
