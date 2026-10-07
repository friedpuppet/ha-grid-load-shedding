# grid-load-shedding — Home Assistant integration

Subproject of `homeassistant` (see `../AGENTS.md`). This is the HA custom integration
**`grid_load_shedding`** ("Grid Load Shedding"). It detects utility-grid loss from a voltage
sensor (with hold and fallback) and sheds/restores flagged switches. It generalizes what used to be the
template helper `binary_sensor.e_elektrika`, the 18 `switch.*_vimikati_bez_merezhi` template switches,
the `power_shed_heavy_loads` automation and the `sensor.vimkneno_cherez_vidkliuchennia` trigger
sensor on this HA instance (see `../electricity.md`).

## Status

- Code and tests are done (`uv run pytest`: all green against HA 2026.8.3).
- Local git only. The **GitHub repo doesn't exist yet**: the user creates it (the PAT in
  `~/.config/github/token` is scoped to the two ha-server-monitor repos only). `manifest.json`,
  `README` and `LICENSE` assume `github.com/friedpuppet/ha-grid-load-shedding`; change them if it lands
  elsewhere (e.g. the `ha-linux-monitoring` org).
- **Not installed on the live HA yet.** The cutover plan is in `PLAN.md` ("Перехід на живому HA").
  Do it only on the user's go-ahead.

## Layout

| Path | What |
|---|---|
| `custom_components/grid_load_shedding/grid.py` | `GridMonitor`: grid-presence state machine (voltage → hold timer → fallback). No entities, unit-testable. |
| `.../shedder.py` | `Shedder`: shed on `on → off`, restore after the delay, persisted list (`Store`, key `grid_load_shedding.<entry_id>`). |
| `.../__init__.py` | Wires the monitor and shedder into `runtime_data`, registers the `forget`/`restore_now` services, reloads on options or subentry change. |
| `.../config_flow.py` | Entry flow (voltage/threshold/hold/fallback), options flow, `load` subentry flow. |
| `.../binary_sensor.py` `sensor.py` `number.py` `button.py` | Entities on the entry's own "Grid" service device. |
| `.../switch.py` | Per-load "Shed on grid loss" flag, attached to the *load's* device via `entity.device_entry = async_entity_id_to_device(...)`. |
| `tests/` | pytest-homeassistant-custom-component. The loads are optimistic template switches. |

## Design notes

- Loads are stored in subentry data as the **entity registry id** (UUID) when available, so they
  survive entity_id renames. They resolve via `er.async_resolve_entity_id` at setup.
- Linking an entity to another integration's device: `async_device_info_to_link_from_entity` is
  **deprecated and returns None** in HA 2026.8. Set `entity.device_entry` instead, as core helpers do.
- Adding or removing a subentry fires the entry's update listeners, so the single
  `add_update_listener(reload)` covers both options and loads. Don't switch to
  `OptionsFlowWithReload`: HA raises if it's combined with update listeners.
- The shed list is flushed synchronously in `async_unload_entry`. A delayed `Store` save alone lost
  the list across a reload (caught by `test_shed_list_survives_reload`).
- Grid listeners are also called on source-only changes (`old == new`), so the `source` attribute stays
  current. `Shedder` ignores those.

## Dev

```sh
cd ~/work/claude/homeassistant/grid-load-shedding
uv sync            # Python ≥3.14.2, HA pinned via pytest-homeassistant-custom-component==0.13.357
uv run pytest -q
```

To bump the HA version under test, pick the PHCC release whose `requires_dist` pins the wanted
`homeassistant==` version. Check it on PyPI.
