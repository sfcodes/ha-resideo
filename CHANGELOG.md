# Changelog

## EIM Duct Temperature Probes, HVAC Stage Relays & System Diagnostics

### Added
- **EIM Duct Temperature Probes (`sensor`)**:
  - Exposed **Discharge duct temperature** (`DischargeTemperature`), **Return duct temperature** (`ReturnTemperature`), and **Duct temperature delta ($\Delta T$)** (`DischargeTemperature - ReturnTemperature`) when probes are wired to an Equipment Interface Module (EIM).
  - Added live SignalR stream merging (`_merge_duct_temperature`) for `DuctTemperature` events (`DischargeTemp` / `ReturnTemp`).
  - Added `eim_accessory` property in `ResideoRooms`.
- **HVAC Stage & Equipment Diagnostics (`binary_sensor` & `sensor`)**:
  - Exposed individual stage running binary sensors (`stage_1_active` through `stage_4_active` from `HeatAndCoolDemand.StagesOn[0..3]`), **Active demand mode** (`HeatAndCoolDemand.Mode`), **Active HVAC stages**, and **EIM status** (`Model 4353`).
- **Local Wall-Unit vs. Priority Averaged Readings (`sensor`)**:
  - Exposed **Thermostat local temperature**, **Thermostat temperature (actual)** (3-decimal precision thermistor reading), and **Thermostat local humidity** directly from the wall unit (`AccessoryId: 0`), distinct from the multi-room priority average (`DisplayedIndoorTemperature`).
- **Alert Thresholds & Maintenance Diagnostics (`sensor`)**:
  - Exposed **High/Low temperature alert thresholds** (`FaultThresholds.TemperatureHighThreshold` / `TemperatureLowThreshold`), **High/Low humidity alert thresholds** (`HumidityHighThreshold` / `HumidityLowThreshold`), **Air filter life** (`RestartDays`), **Backlight intensity**, and **Last connectivity change** timestamp.
