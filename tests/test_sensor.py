"""Tests for sensor exposure, units, defaults, and registry migration."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass, replace
from decimal import Decimal
from types import ModuleType, SimpleNamespace

from .helpers import INTEGRATION, load_module


def _install_sensor_stubs() -> None:
    """Install the Home Assistant surface required to import sensor.py."""
    homeassistant = sys.modules.setdefault("homeassistant", ModuleType("homeassistant"))
    homeassistant.__path__ = []

    components = ModuleType("homeassistant.components")
    components.__path__ = []
    sys.modules[components.__name__] = components
    sensor_component = ModuleType("homeassistant.components.sensor")

    class SensorDeviceClass:
        CURRENT = "current"
        ENERGY = "energy"
        GAS = "gas"
        POWER = "power"
        VOLTAGE = "voltage"
        WATER = "water"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class SensorEntity:
        pass

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        translation_key: str | None = None
        device_class: str | None = None
        native_unit_of_measurement: str | None = None
        suggested_unit_of_measurement: str | None = None
        state_class: str | None = None
        entity_category: str | None = None
        entity_registry_enabled_default: bool = True
        icon: str | None = None

    sensor_component.SensorDeviceClass = SensorDeviceClass
    sensor_component.SensorEntity = SensorEntity
    sensor_component.SensorEntityDescription = SensorEntityDescription
    sensor_component.SensorStateClass = SensorStateClass
    sys.modules[sensor_component.__name__] = sensor_component

    ha_const = sys.modules.setdefault(
        "homeassistant.const", ModuleType("homeassistant.const")
    )
    ha_const.CONF_HOST = "host"
    ha_const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    ha_const.Platform = SimpleNamespace(SENSOR="sensor")
    ha_const.UnitOfElectricCurrent = SimpleNamespace(AMPERE="A")
    ha_const.UnitOfElectricPotential = SimpleNamespace(VOLT="V")
    ha_const.UnitOfEnergy = SimpleNamespace(KILO_WATT_HOUR="kWh")
    ha_const.UnitOfPower = SimpleNamespace(WATT="W", KILO_WATT="kW")
    ha_const.UnitOfVolume = SimpleNamespace(CUBIC_METERS="m3")

    core = sys.modules.setdefault(
        "homeassistant.core", ModuleType("homeassistant.core")
    )
    core.callback = lambda function: function
    core.HomeAssistant = object

    helpers = sys.modules.setdefault(
        "homeassistant.helpers", ModuleType("homeassistant.helpers")
    )
    helpers.__path__ = []
    device_registry = ModuleType("homeassistant.helpers.device_registry")

    class DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    device_registry.DeviceInfo = DeviceInfo
    device_registry.async_get = lambda _hass: None
    sys.modules[device_registry.__name__] = device_registry
    helpers.device_registry = device_registry

    entity_registry = ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda _hass: None
    sys.modules[entity_registry.__name__] = entity_registry
    helpers.entity_registry = entity_registry

    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = object
    sys.modules[entity_platform.__name__] = entity_platform

    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return True

    update_coordinator.CoordinatorEntity = CoordinatorEntity
    sys.modules[update_coordinator.__name__] = update_coordinator


_install_sensor_stubs()
package = sys.modules["custom_components.ecoflow_p1"]
package.EcoFlowP1ConfigEntry = object
coordinator_module = ModuleType("custom_components.ecoflow_p1.coordinator")
coordinator_module.EcoFlowP1Coordinator = object
sys.modules[coordinator_module.__name__] = coordinator_module
models = load_module("custom_components.ecoflow_p1.models")
load_module("custom_components.ecoflow_p1.mbus")
sensor = load_module("custom_components.ecoflow_p1.sensor")


def _data_with_channel(channel) -> object:
    """Create coordinator data with one M-Bus channel."""
    telegram = models.ParsedTelegram(
        header="/ISK5\\meter",
        obis={},
        meter_info=models.MeterInfo(mbus_channels={channel.channel: channel}),
    )
    return models.EcoFlowP1Data(
        telegram=telegram,
        serial="P1-123",
        firmware_version="1.1.07",
        timeout_times=1,
        crc_error_times=0,
        total_times=2,
    )


class FakeRegistry:
    """Record entity-registry migrations and removals."""

    def __init__(self, entries, registry_entries=None):
        self.entries = dict(entries)
        self.registry_entries = registry_entries or {}
        self.removed = []
        self.updated = []
        self.unit_updates = []
        self.option_updates = []

    def async_get_entity_id(self, _domain, _platform, unique_id):
        return self.entries.get(unique_id)

    def async_remove(self, entity_id):
        self.removed.append(entity_id)

    def async_get(self, entity_id):
        return self.registry_entries.get(entity_id)

    def async_update_entity(
        self, entity_id, *, new_unique_id=None, unit_of_measurement="unchanged"
    ):
        if new_unique_id is not None:
            self.updated.append((entity_id, new_unique_id))
        if unit_of_measurement != "unchanged":
            self.unit_updates.append((entity_id, unit_of_measurement))

    def async_update_entity_options(self, entity_id, domain, options):
        self.option_updates.append((entity_id, domain, options))


class SensorTests(unittest.TestCase):
    """Verify deterministic entity exposure and compatibility migrations."""

    def test_gas_missing_or_zero_readings_are_unavailable(self) -> None:
        """Do not publish temporary zero totals or missing gas readings."""
        channel = models.MBusChannel(
            1, device_type=3, delivered=Decimal("123.456"), unit="m3"
        )
        coordinator = SimpleNamespace(data=_data_with_channel(channel))
        description = sensor._mbus_description(channel, "gas")
        entity = sensor.EcoFlowP1Sensor(
            coordinator,
            SimpleNamespace(unique_id="P1-123", entry_id="test"),
            description,
            "dongle-device-id",
        )
        for value in (Decimal("123.456"), Decimal("0"), None, Decimal("123.457")):
            with self.subTest(value=value):
                coordinator.data = _data_with_channel(replace(channel, delivered=value))
                expected = value if value else None
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(entity.available, expected is not None)

        coordinator.data = replace(
            coordinator.data,
            telegram=replace(coordinator.data.telegram, meter_info=models.MeterInfo()),
        )
        self.assertIsNone(entity.native_value)
        self.assertFalse(entity.available)
        self.assertEqual(description.native_unit_of_measurement, "m3")
        self.assertEqual(description.state_class, "total_increasing")
        self.assertEqual(entity._attr_unique_id, "P1-123_mbus_gas_1")

    def test_other_mbus_zero_readings_remain_available(self) -> None:
        """Keep zero totals valid for water and energy meters."""
        for kind, device_type, unit in (("water", 7, "m3"), ("energy", 4, "kWh")):
            with self.subTest(kind=kind):
                channel = models.MBusChannel(
                    1, device_type=device_type, delivered=Decimal("0"), unit=unit
                )
                self.assertEqual(
                    sensor._value_for_description(
                        _data_with_channel(channel),
                        sensor._mbus_description(channel, kind),
                    ),
                    Decimal("0"),
                )

    def test_demand_sensors_and_history_attributes(self) -> None:
        """Expose watt scalar states and JSON-compatible monthly peak records."""
        parser = load_module("custom_components.ecoflow_p1.parser")
        telegram = parser.parse_telegram(
            "/FLU5\\meter\n1-0:1.7.0(00.123*kW)\n"
            "1-0:1.4.0(00.389*kW)\n"
            "1-0:1.6.0(260902200000S)(03.600*kW)\n"
            "0-0:98.1.0(2)(1-0:1.6.0)(1-0:1.6.0)"
            "(250901000000S)(250831100000S)(04.157*kW)"
            "(251001000000S)(250905213000S)(04.577*kW)\n!"
        )
        data = replace(_data_with_channel(models.MBusChannel(1)), telegram=telegram)
        descriptions = {item.key: item for item in sensor.SENSOR_DESCRIPTIONS}
        for key, expected in (
            ("current_quarter_average", "389"),
            ("monthly_peak", "3600"),
        ):
            description = descriptions[key]
            self.assertEqual(description.translation_key, key)
            self.assertEqual(description.device_class, "power")
            self.assertEqual(description.state_class, "measurement")
            self.assertEqual(description.native_unit_of_measurement, "W")
            self.assertEqual(description.suggested_unit_of_measurement, "W")
            self.assertFalse(description.entity_registry_enabled_default)
            self.assertEqual(
                sensor._value_for_description(data, description), Decimal(expected)
            )
            for path in ("strings.json", "translations/en.json"):
                translations = json.loads((INTEGRATION / path).read_text())
                self.assertIn(key, translations["entity"]["sensor"])
        entity = sensor.EcoFlowP1Sensor(
            SimpleNamespace(data=data),
            SimpleNamespace(unique_id="test", entry_id="entry"),
            descriptions["monthly_peak"],
            "parent",
        )
        self.assertEqual(entity._attr_unique_id, "test_monthly_peak")
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "peak_timestamp": "260902200000S",
                "peak_datetime": "2026-09-02T20:00:00+02:00",
                "history_count": 2,
                "peaks": [
                    {
                        "period_timestamp": "250901000000S",
                        "peak_timestamp": "250831100000S",
                        "period_datetime": "2025-09-01T00:00:00+02:00",
                        "peak_datetime": "2025-08-31T10:00:00+02:00",
                        "peak_kw": 4.157,
                    },
                    {
                        "period_timestamp": "251001000000S",
                        "peak_timestamp": "250905213000S",
                        "period_datetime": "2025-10-01T00:00:00+02:00",
                        "peak_datetime": "2025-09-05T21:30:00+02:00",
                        "peak_kw": 4.577,
                    },
                ],
            },
        )
        json.dumps(entity.extra_state_attributes, allow_nan=False)
        invalid_telegram = replace(
            telegram,
            obis={"1-0:1.6.0": models.ObisValue(("000000000000W", "03.600*kW"))},
            demand_history=(models.DemandPeak("", "991332256199S", Decimal("4.157")),),
        )
        entity.coordinator.data = replace(data, telegram=invalid_telegram)
        attributes = entity.extra_state_attributes
        self.assertEqual(entity.native_value, Decimal("3600"))
        self.assertIsNone(attributes["peak_datetime"])
        self.assertEqual(attributes["peak_timestamp"], "000000000000W")
        self.assertEqual(attributes["history_count"], 1)
        self.assertIsNone(attributes["peaks"][0]["period_datetime"])
        self.assertIsNone(attributes["peaks"][0]["peak_datetime"])
        self.assertEqual(attributes["peaks"][0]["peak_kw"], 4.157)
        json.dumps(attributes, allow_nan=False)
        entity.coordinator.data = _data_with_channel(models.MBusChannel(1))
        self.assertIsNone(entity.native_value)
        self.assertFalse(entity.available)
        self.assertEqual(
            entity.extra_state_attributes, {"history_count": 0, "peaks": []}
        )

    def test_demand_rejects_invalid_readings(self) -> None:
        """Missing, malformed and wrong-unit values do not become sensor states."""
        descriptions = {item.key: item for item in sensor.SENSOR_DESCRIPTIONS}
        data = _data_with_channel(models.MBusChannel(1))
        for key in ("monthly_peak", "current_quarter_average"):
            description = descriptions[key]
            for raw in (
                "",
                "bad*kW",
                "1*W",
                "1*kWh",
                "1",
                "NaN*kW",
                "Infinity*kW",
                "-1*kW",
            ):
                with self.subTest(key=key, raw=raw):
                    groups = ("000000000000W", raw) if key == "monthly_peak" else (raw,)
                    telegram = replace(
                        data.telegram, obis={description.obis: models.ObisValue(groups)}
                    )
                    self.assertIsNone(
                        sensor._value_for_description(
                            replace(data, telegram=telegram), description
                        )
                    )

    def test_demand_sensors_migrate_legacy_kw_preference(self) -> None:
        """Clear an automatic legacy kW preference for demand sensors too."""
        registry = FakeRegistry(
            {"test_monthly_peak": "sensor.peak"},
            {"sensor.peak": SimpleNamespace(unit_of_measurement="kW", options={})},
        )
        sensor.er.async_get = lambda _hass: registry
        sensor._migrate_legacy_power_units(None, "test")
        self.assertEqual(registry.unit_updates, [("sensor.peak", None)])

    def test_exposes_every_static_dsmr_description(self) -> None:
        """Register static DSMR entities even when a telegram omits their values."""
        data = _data_with_channel(models.MBusChannel(1, device_type=3, unit="m3"))
        descriptions = sensor._available_descriptions(data)
        keys = {description.key for description in descriptions}

        self.assertTrue(
            {description.key for description in sensor.SENSOR_DESCRIPTIONS} <= keys
        )
        for phase in (1, 2, 3):
            self.assertIn(f"voltage_l{phase}", keys)
            self.assertIn(f"current_l{phase}", keys)
            self.assertIn(f"power_import_l{phase}", keys)
            self.assertIn(f"power_export_l{phase}", keys)

    def test_all_power_descriptions_use_watts(self) -> None:
        """Keep aggregate and per-phase power readings in watts."""
        power_descriptions = [
            description
            for description in sensor.SENSOR_DESCRIPTIONS
            if description.device_class == "power"
        ]
        self.assertTrue(power_descriptions)
        for description in power_descriptions:
            with self.subTest(key=description.key):
                self.assertEqual(description.native_unit_of_measurement, "W")
                self.assertEqual(description.suggested_unit_of_measurement, "W")
                self.assertEqual(description.value_multiplier, Decimal(1000))

    def test_three_phase_mode_enables_l2_and_l3_by_default(self) -> None:
        """Use the manual phase choice when registering new entities."""
        data = _data_with_channel(models.MBusChannel(1, device_type=3, unit="m3"))
        descriptions = sensor._available_descriptions(data, "three")

        phase_descriptions = [
            item for item in descriptions if item.key.endswith(("_l2", "_l3"))
        ]
        self.assertTrue(phase_descriptions)
        self.assertTrue(
            all(item.entity_registry_enabled_default for item in phase_descriptions)
        )

    def test_child_devices_use_parent_device_registry_id(self) -> None:
        """Link child devices with the non-deprecated via_device_id field."""
        data = _data_with_channel(
            models.MBusChannel(
                1, device_type=3, delivered=Decimal("123.456"), unit="m3"
            )
        )
        coordinator = SimpleNamespace(data=data)
        descriptions = sensor._available_descriptions(data)

        for description in (
            next(item for item in descriptions if item.key == "power_import"),
            next(item for item in descriptions if item.key == "mbus_gas_1"),
        ):
            with self.subTest(key=description.key):
                device_info = sensor._device_info(
                    coordinator, "P1-123", description, "dongle-device-id"
                )

                self.assertEqual(device_info["via_device_id"], "dongle-device-id")
                self.assertNotIn("via_device", device_info)

    def test_clears_automatic_legacy_kw_preference(self) -> None:
        """Stop Home Assistant converting native watts back to the former kW unit."""
        power = next(
            item for item in sensor.SENSOR_DESCRIPTIONS if item.key == "power_import"
        )
        unique_id = f"P1-123_{power.key}"
        registry = FakeRegistry(
            {unique_id: "sensor.power_consumption"},
            {
                "sensor.power_consumption": SimpleNamespace(
                    unit_of_measurement="kW",
                    options={
                        "sensor": {"unit_of_measurement": "kW"},
                        "sensor.private": {"suggested_unit_of_measurement": "kW"},
                    },
                )
            },
        )
        sensor.er.async_get = lambda _hass: registry

        sensor._migrate_legacy_power_units(None, "P1-123")

        self.assertEqual(
            registry.option_updates,
            [("sensor.power_consumption", "sensor.private", None)],
        )
        self.assertEqual(
            registry.unit_updates,
            [("sensor.power_consumption", None)],
        )
        self.assertEqual(
            registry.registry_entries["sensor.power_consumption"].options["sensor"],
            {"unit_of_measurement": "kW"},
        )

    def test_homey_style_diagnostic_icons(self) -> None:
        """Use the same diagnostic symbols as Homey P1."""
        descriptions = {item.key: item for item in sensor.SENSOR_DESCRIPTIONS}
        self.assertEqual(descriptions["active_tariff"].icon, "mdi:counter")
        self.assertEqual(descriptions["power_failures"].icon, "mdi:flash-alert")
        self.assertEqual(descriptions["voltage_sags_l1"].icon, "mdi:sine-wave")

    def test_l1_enabled_and_l2_l3_disabled_by_default(self) -> None:
        """Enable useful L1 readings while keeping absent phases quiet."""
        descriptions = {item.key: item for item in sensor.SENSOR_DESCRIPTIONS}
        for metric in ("voltage", "current", "power_import", "power_export"):
            self.assertTrue(
                descriptions[f"{metric}_l1"].entity_registry_enabled_default
            )
            self.assertFalse(
                descriptions[f"{metric}_l2"].entity_registry_enabled_default
            )
            self.assertFalse(
                descriptions[f"{metric}_l3"].entity_registry_enabled_default
            )

    def test_migrates_legacy_gas_unique_id(self) -> None:
        """Preserve the old entity when no replacement exists yet."""
        channel = models.MBusChannel(
            1, device_type=3, delivered=Decimal("12.3"), unit="m3"
        )
        registry = FakeRegistry({"P1-123_gas_consumption_1": "sensor.old_gas"})
        sensor.er.async_get = lambda _hass: registry

        sensor._migrate_legacy_mbus_entities(
            None, "P1-123", _data_with_channel(channel)
        )

        self.assertEqual(
            registry.updated,
            [("sensor.old_gas", "P1-123_mbus_gas_1")],
        )
        self.assertEqual(registry.removed, [])

    def test_removes_legacy_entity_when_replacement_exists(self) -> None:
        """Remove a duplicate even when the current telegram omits its channel."""
        registry = FakeRegistry(
            {
                "P1-123_gas_consumption_1": "sensor.old_gas",
                "P1-123_mbus_gas_1": "sensor.gas_consumption",
            }
        )
        sensor.er.async_get = lambda _hass: registry

        sensor._migrate_legacy_mbus_entities(
            None,
            "P1-123",
            _data_with_channel(models.MBusChannel(5)),
        )

        self.assertEqual(registry.updated, [])
        self.assertEqual(registry.removed, ["sensor.old_gas"])

    def test_keeps_legacy_entity_when_mbus_reading_is_temporarily_absent(self) -> None:
        """Do not erase entity identity based on one incomplete telegram."""
        snapshots = (
            _data_with_channel(models.MBusChannel(5)),
            _data_with_channel(models.MBusChannel(1, device_type=3)),
        )
        for data in snapshots:
            with self.subTest(channels=data.telegram.meter_info.mbus_channels):
                registry = FakeRegistry({"P1-123_gas_consumption_1": "sensor.old_gas"})
                sensor.er.async_get = lambda _hass, current=registry: current

                sensor._migrate_legacy_mbus_entities(None, "P1-123", data)

                self.assertEqual(registry.updated, [])
                self.assertEqual(registry.removed, [])


if __name__ == "__main__":
    unittest.main()
