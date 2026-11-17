# Changelog

## Room Priority Controls & Multi-Room Averaging

### Added
- **Room Priority Control (`switch` & `select`)**:
  - Added per-room priority switches (`switch.*_room_priority`) on every wireless room sensor sub-device and a built-in thermostat room priority switch (`switch.*_include_thermostat_room_in_priority`) under **Controls** to dynamically include or exclude rooms from temperature averaging (`SelectedRooms`).
  - Added a thermostat Priority Mode selector (`select.*_priority_mode`) under **Controls** to switch between `PickARoom` and `FollowMe`.
  - Added shared optimistic override coalescing (`priority_override`) in `ResideoDataUpdateCoordinator` so back-to-back room toggles compose into a single coherent `/priority` payload without clobbering each other during the cloud reconciliation window.
  - Added `sensor.*_active_priority_rooms` displaying the human-readable list of rooms currently participating in priority averaging.
