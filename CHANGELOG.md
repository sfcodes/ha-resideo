# Changelog

## Humidifier & Dehumidifier Setpoint Controls & Fan Mode Selector

### Added
- **Humidifier & Dehumidifier Setpoint Sliders (`number.py`, `client.py`, `configuration.py`)**:
  - Exposed writable **Humidifier setpoint** (`number.*_humidifier_setpoint`) and **Dehumidifier setpoint** (`number.*_dehumidifier_setpoint`) slider controls under **Controls** (`PUT .../humidifier/setpoint` and `PUT .../dehumidifier/setpoint` with `{"SetPointValue": <int>}`), dynamically bounded by `HumidificationMinimumSetpoint` / `HumidificationMaximumSetpoint` and `DehumidificationMinimumSetpoint` / `DehumidificationMaximumSetpoint` in `/configuration`.
- **Standalone Fan Mode Control (`select.py`)**:
  - Exposed a standalone **Fan mode** selector (`select.*_fan_mode`) under **Controls** for switching between `Auto`, `Circulate`, and `On` directly from device and area overview cards (`PUT .../fanSwitch`).
- **Whole-Home Humidifier & Dehumidifier Entities (`binary_sensor` & `sensor`)**:
  - Exposed live running binary sensors for **Humidifier** (`HumidifierRequest`) and **Dehumidifier** (`DehumidifierRequest`), plus **Dehumidification away active** (`DehumAway.Active`).
  - Exposed target setpoints (`HumidifierSetpoint`, `DehumidifierSetpoint`), **Humidifier deadband**, **Dehumidifier away setpoint**, **Dehumidifier away cool setpoint**, and **Windows protection** (frost level `1–10` and condensation protection percentage).
  - Added live SignalR stream merging (`_merge_humidification`) for `Humidification` push events.
