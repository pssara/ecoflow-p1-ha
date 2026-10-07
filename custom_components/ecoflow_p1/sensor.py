"""Sensor platform for the EcoFlow P1 Energy Tracker."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    Platform,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EcoFlowP1ConfigEntry
from .const import DOMAIN, PHASE_MODE_THREE
from .coordinator import EcoFlowP1Coordinator
from .mbus import classify_mbus_channel, mbus_device_type_name
from .models import EcoFlowP1Data, MBusChannel, parse_dsmr_datetime

DeviceGroup = Literal["dongle", "electricity", "mbus"]


@dataclass(frozen=True, kw_only=True)
class EcoFlowP1SensorDescription(SensorEntityDescription):
    """Describe an EcoFlow P1 sensor."""

    obis: str | None = None
    expected_unit: str | None = None
    metadata_key: str | None = None
    meter_channel: int | None = None
    device_group: DeviceGroup = "electricity"
    value_multiplier: Decimal = Decimal(1)


def _energy(key: str, obis: str, *, enabled: bool = True) -> EcoFlowP1SensorDescription:
    return EcoFlowP1SensorDescription(
        key=key,
        translation_key=key,
        obis=obis,
        expected_unit="kWh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=enabled,
    )


def _power(key: str, obis: str, *, enabled: bool = True) -> EcoFlowP1SensorDescription:
    return EcoFlowP1SensorDescription(
        key=key,
        translation_key=key,
        obis=obis,
        expected_unit="kW",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        value_multiplier=Decimal(1000),
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
    )


def _count(key: str, obis: str, icon: str) -> EcoFlowP1SensorDescription:
    return EcoFlowP1SensorDescription(
        key=key,
        translation_key=key,
        obis=obis,
        icon=icon,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


SENSOR_DESCRIPTIONS: tuple[EcoFlowP1SensorDescription, ...] = (
    _energy("energy_import_tariff_1", "1-0:1.8.1"),
    _energy("energy_import_tariff_2", "1-0:1.8.2"),
    _energy("energy_export_tariff_1", "1-0:2.8.1"),
    _energy("energy_export_tariff_2", "1-0:2.8.2"),
    _power("power_import", "1-0:1.7.0"),
    _power("power_export", "1-0:2.7.0"),
    _power("current_quarter_average", "1-0:1.4.0", enabled=False),
    _power("monthly_peak", "1-0:1.6.0", enabled=False),
    EcoFlowP1SensorDescription(
        key="active_tariff",
        translation_key="active_tariff",
        obis="0-0:96.14.0",
        icon="mdi:counter",
        entity_registry_enabled_default=False,
    ),
    _count("power_failures", "0-0:96.7.21", "mdi:flash-alert"),
    _count("long_power_failures", "0-0:96.7.9", "mdi:flash-alert"),
    *tuple(
        EcoFlowP1SensorDescription(
            key=f"voltage_l{phase}",
            translation_key=f"voltage_l{phase}",
            obis=f"1-0:{code}.7.0",
            expected_unit="V",
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=phase == 1,
        )
        for phase, code in ((1, 32), (2, 52), (3, 72))
    ),
    *tuple(
        EcoFlowP1SensorDescription(
            key=f"current_l{phase}",
            translation_key=f"current_l{phase}",
            obis=f"1-0:{code}.7.0",
            expected_unit="A",
            device_class=SensorDeviceClass.CURRENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=phase == 1,
        )
        for phase, code in ((1, 31), (2, 51), (3, 71))
    ),
    *tuple(
        description
        for phase, import_code, export_code in (
            (1, 21, 22),
            (2, 41, 42),
            (3, 61, 62),
        )
        for description in (
            _power(
                f"power_import_l{phase}",
                f"1-0:{import_code}.7.0",
                enabled=phase == 1,
            ),
            _power(
                f"power_export_l{phase}",
                f"1-0:{export_code}.7.0",
                enabled=phase == 1,
            ),
        )
    ),
    *tuple(
        description
        for phase, code in ((1, 32), (2, 52), (3, 72))
        for description in (
            _count(
                f"voltage_sags_l{phase}",
                f"1-0:{code}.32.0",
                "mdi:sine-wave",
            ),
            _count(
                f"voltage_swells_l{phase}",
                f"1-0:{code}.36.0",
                "mdi:sine-wave",
            ),
        )
    ),
    *tuple(
        EcoFlowP1SensorDescription(
            key=key,
            translation_key=key,
            metadata_key=key,
            device_group="dongle",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            state_class=SensorStateClass.TOTAL_INCREASING,
        )
        for key in ("timeout_times", "crc_error_times", "total_times")
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowP1ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors and discover optional phases or M-Bus meters later."""
    coordinator = entry.runtime_data.coordinator
    base_id = entry.unique_id or entry.entry_id
    dongle_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **_dongle_device_info(coordinator, base_id),
    )
    _migrate_legacy_power_units(hass, base_id)
    _migrate_legacy_mbus_entities(hass, base_id, coordinator.data)
    known: set[str] = set()

    @callback
    def discover_entities() -> None:
        descriptions = _available_descriptions(coordinator.data, coordinator.phase_mode)
        new = [
            description for description in descriptions if description.key not in known
        ]
        if not new:
            return
        known.update(description.key for description in new)
        async_add_entities(
            EcoFlowP1Sensor(coordinator, entry, description, dongle_device.id)
            for description in new
        )

    discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(discover_entities))


def _available_descriptions(
    data: EcoFlowP1Data, phase_mode: str | None = None
) -> list[EcoFlowP1SensorDescription]:
    """Return all DSMR descriptions plus discovered M-Bus readings."""
    descriptions = [
        replace(description, entity_registry_enabled_default=True)
        if phase_mode == PHASE_MODE_THREE and description.key.endswith(("_l2", "_l3"))
        else description
        for description in SENSOR_DESCRIPTIONS
    ]

    for channel in data.telegram.meter_info.mbus_channels.values():
        kind = classify_mbus_channel(channel)
        if channel.delivered is None or kind is None:
            continue
        descriptions.append(_mbus_description(channel, kind))
    return descriptions


def _migrate_legacy_mbus_entities(
    hass: HomeAssistant, base_id: str, data: EcoFlowP1Data
) -> None:
    """Move legacy gas entities only when their replacement is known."""
    registry = er.async_get(hass)
    channels = data.telegram.meter_info.mbus_channels

    # Versions before 0.2.0 only supported gas on channels 1 through 4 and used
    # gas_consumption_<channel> as the unique-ID suffix.
    for channel_number in range(1, 5):
        old_unique_id = f"{base_id}_gas_consumption_{channel_number}"
        old_entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, old_unique_id
        )
        if old_entity_id is None:
            continue

        replacement_exists = any(
            registry.async_get_entity_id(
                Platform.SENSOR,
                DOMAIN,
                f"{base_id}_mbus_{kind}_{channel_number}",
            )
            is not None
            for kind in ("gas", "water", "energy")
        )
        if replacement_exists:
            registry.async_remove(old_entity_id)
            continue

        channel = channels.get(channel_number)
        kind = classify_mbus_channel(channel) if channel is not None else None
        if channel is None or channel.delivered is None or kind is None:
            # An M-Bus reading can be absent from an otherwise valid telegram.
            # Do not delete registry entries based on one transient snapshot.
            continue

        new_unique_id = f"{base_id}_mbus_{kind}_{channel_number}"
        registry.async_update_entity(old_entity_id, new_unique_id=new_unique_id)


def _migrate_legacy_power_units(hass: HomeAssistant, base_id: str) -> None:
    """Remove Home Assistant's automatic preference for the former kW unit."""
    registry = er.async_get(hass)
    for description in SENSOR_DESCRIPTIONS:
        if description.device_class != SensorDeviceClass.POWER:
            continue

        unique_id = f"{base_id}_{description.key}"
        entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id)
        if entity_id is None or (entry := registry.async_get(entity_id)) is None:
            continue

        # Home Assistant stores an integration's former native unit privately
        # so it can preserve presentation across an integration unit change.
        # Clear only that automatic preference; a user's explicit sensor unit
        # option remains untouched.
        private_options = entry.options.get("sensor.private")
        if (
            isinstance(private_options, Mapping)
            and private_options.get("suggested_unit_of_measurement")
            == UnitOfPower.KILO_WATT
        ):
            new_private_options = dict(private_options)
            new_private_options.pop("suggested_unit_of_measurement")
            registry.async_update_entity_options(
                entity_id, "sensor.private", new_private_options or None
            )

        if entry.unit_of_measurement == UnitOfPower.KILO_WATT:
            registry.async_update_entity(entity_id, unit_of_measurement=None)


def _mbus_description(
    channel: MBusChannel, kind: Literal["gas", "water", "energy"]
) -> EcoFlowP1SensorDescription:
    """Create a sensor description for a classified M-Bus reading."""
    if kind == "gas":
        device_class = SensorDeviceClass.GAS
        unit = UnitOfVolume.CUBIC_METERS
    elif kind == "water":
        device_class = SensorDeviceClass.WATER
        unit = UnitOfVolume.CUBIC_METERS
    else:
        device_class = SensorDeviceClass.ENERGY
        unit = channel.unit

    return EcoFlowP1SensorDescription(
        key=f"mbus_{kind}_{channel.channel}",
        translation_key=f"mbus_{kind}",
        meter_channel=channel.channel,
        device_group="mbus",
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


def _value_for_description(
    data: EcoFlowP1Data, description: EcoFlowP1SensorDescription
) -> Decimal | int | None:
    """Read the current value described by an entity description."""
    if description.metadata_key is not None:
        value = getattr(data, description.metadata_key, None)
        return value if isinstance(value, int) else None
    if description.meter_channel is not None:
        channel = data.telegram.meter_info.mbus_channels.get(description.meter_channel)
        if channel is None or channel.delivered is None:
            return None
        # Some meters temporarily report zero for the cumulative gas total.
        if description.device_class == SensorDeviceClass.GAS and channel.delivered == 0:
            return None
        return channel.delivered
    if description.obis is None:
        return None
    if description.key == "monthly_peak":
        item = data.telegram.obis.get(description.obis)
        if item is None or len(item.values) != 2:
            return None
    value = data.telegram.decimal(description.obis, description.expected_unit)
    if (
        description.key in {"monthly_peak", "current_quarter_average"}
        and value is not None
        and (not value.is_finite() or value < 0)
    ):
        return None
    return value * description.value_multiplier if value is not None else None


def _datetime_attribute(timestamp: str) -> str | None:
    """Serialize an aware datetime for Home Assistant's JSON attributes."""
    value = parse_dsmr_datetime(timestamp)
    return value.isoformat() if value is not None else None


class EcoFlowP1Sensor(CoordinatorEntity[EcoFlowP1Coordinator], SensorEntity):
    """Representation of one EcoFlow P1 measurement."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowP1Coordinator,
        entry: EcoFlowP1ConfigEntry,
        description: EcoFlowP1SensorDescription,
        parent_device_id: str,
    ) -> None:
        """Initialize an EcoFlow P1 sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        base_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base_id}_{description.key}"
        self._attr_device_info = _device_info(
            coordinator, base_id, description, parent_device_id
        )

    @property
    def available(self) -> bool:
        """Return whether the coordinator and this optional reading are available."""
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> Decimal | int | None:
        """Return the latest sensor value."""
        return _value_for_description(self.coordinator.data, self.entity_description)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose raw identifiers and protocol details useful for diagnostics."""
        info = self.coordinator.data.telegram.meter_info
        if self.entity_description.meter_channel is not None:
            channel = info.mbus_channels.get(self.entity_description.meter_channel)
            if channel is None:
                return None
            attributes: dict[str, Any] = {"mbus_channel": channel.channel}
            if channel.equipment_id:
                attributes["equipment_id"] = channel.equipment_id
            if name := mbus_device_type_name(channel.device_type):
                attributes["mbus_device_type"] = name
            if channel.timestamp:
                attributes["reading_timestamp"] = channel.timestamp
            return attributes
        if self.entity_description.device_group == "electricity":
            attributes = {}
            if self.entity_description.key == "monthly_peak":
                telegram = self.coordinator.data.telegram
                peak = telegram.obis.get("1-0:1.6.0")
                if peak is not None and len(peak.values) == 2:
                    attributes["peak_timestamp"] = peak.values[0]
                    attributes["peak_datetime"] = _datetime_attribute(peak.values[0])
                attributes["history_count"] = len(telegram.demand_history)
                attributes["peaks"] = [
                    {
                        "period_timestamp": record.period_timestamp,
                        "peak_timestamp": record.peak_timestamp,
                        "period_datetime": _datetime_attribute(record.period_timestamp),
                        "peak_datetime": _datetime_attribute(record.peak_timestamp),
                        "peak_kw": float(record.peak_kw),
                    }
                    for record in telegram.demand_history
                ]
            if info.electricity_equipment_id:
                attributes["equipment_id"] = info.electricity_equipment_id
            if info.dsmr_version:
                attributes["dsmr_version"] = info.dsmr_version
            return attributes or None
        return None


def _device_info(
    coordinator: EcoFlowP1Coordinator,
    base_id: str,
    description: EcoFlowP1SensorDescription,
    parent_device_id: str,
) -> DeviceInfo:
    """Build the dongle, electricity-meter, or M-Bus child device."""
    data = coordinator.data
    if description.device_group == "dongle":
        return _dongle_device_info(coordinator, base_id)

    info = data.telegram.meter_info
    if description.device_group == "electricity":
        protocol = " ".join(
            part for part in (info.protocol_family, info.dsmr_version) if part
        )
        return DeviceInfo(
            identifiers={(DOMAIN, f"{base_id}:electricity")},
            via_device_id=parent_device_id,
            manufacturer=info.manufacturer,
            model=info.model or "Electricity meter",
            name="Electricity meter",
            serial_number=info.electricity_serial,
            hw_version=protocol or None,
        )

    channel_number = description.meter_channel
    channel = info.mbus_channels.get(channel_number) if channel_number else None
    type_name = mbus_device_type_name(channel.device_type) if channel else None
    type_code = (
        f"ID:{channel.device_type:03d}"
        if channel is not None and channel.device_type is not None
        else None
    )
    model = f"M-Bus {channel_number}"
    if type_code:
        model = f"{model} / {type_code}"
    return DeviceInfo(
        identifiers={(DOMAIN, f"{base_id}:mbus:{channel_number}")},
        via_device_id=parent_device_id,
        model=model,
        name=(type_name or "M-Bus meter").title(),
        serial_number=channel.meter_serial if channel else None,
    )


def _dongle_device_info(coordinator: EcoFlowP1Coordinator, base_id: str) -> DeviceInfo:
    """Build device information for the physical EcoFlow dongle."""
    data = coordinator.data
    return DeviceInfo(
        identifiers={(DOMAIN, base_id)},
        manufacturer="EcoFlow",
        model="P1 Energy Tracker",
        name="EcoFlow P1 Energy Tracker",
        serial_number=data.serial,
        sw_version=data.firmware_version,
        configuration_url=f"http://{coordinator.api.host}/",
    )
