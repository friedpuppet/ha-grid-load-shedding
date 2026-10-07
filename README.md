# Grid Load Shedding

> Personal project, published only so it can be installed through HACS as a custom repository.
> Use it if it's useful to you, but there's no support and no promise of stability.

A Home Assistant integration for homes with a hybrid inverter or UPS. It detects when utility-grid
power is lost, turns off selected heavy loads (kettle, washing machine, air conditioners…) so they
don't drain the battery, and turns the same ones back on once the grid has been back for a while.

## How it decides "grid is present"

- **Voltage sensor**: grid is present while it is above a threshold, typically the inverter's
  grid-input voltage (e.g. 170 V, the lowest the inverter accepts in UPS mode).
- **Short dropouts**: inverter Wi-Fi bridges drop out now and then. While the voltage sensor is
  `unavailable`, the last state is held for *hold time* (default 30 s).
- **Fallback**: after the hold time, an optional fallback binary sensor decides. A good one is a
  ping of a device powered *before* the inverter, which goes dark during an outage. With no fallback,
  or with the fallback unavailable too, the Grid sensor becomes `unavailable` instead of `off`, so a dead
  sensor is never mistaken for an outage.

## What it does with loads

Add loads under the integration's entry with **Add load**: pick any `switch`. Each load gets a
**Shed on grid loss** switch on the load's own device, on by default.

- **Grid goes from on to off**: every load whose flag is on and which is currently on gets turned off and
  remembered. Loads that were already off stay off and are not remembered.
- **Grid has been back for *Restore delay* seconds**: the remembered loads are turned back on.
  If the grid drops again during the delay, the restore is cancelled.
- **Restarts**: the remembered list survives Home Assistant restarts and entry reloads.

### Schedule window (optional, per load)

A load can also get a schedule window, e.g. a boiler that heats 01:00–07:00. Set it when adding the
load or later with **Reconfigure**; clear both fields to remove it. A load with a window gets a
**Run on schedule** switch, on by default. While that switch is on:

| Event | Action |
|---|---|
| Window start, grid present | turn on |
| Window start, no grid | remember the missed start |
| Grid back (after *Restore delay*), inside the window | turn on, if it was shed or its start was missed |
| Grid back, window already over | leave off, drop from the list |
| Window end | turn off, even without grid; drop from the list and clear any missed start |

- Turning **Run on schedule** on inside the window (with grid) starts the load right away.
- Manually switching the load off inside the window is respected.
- With **Run on schedule** off, the load behaves like one without a window.
- Windows may cross midnight (e.g. 23:00–02:00). Times are Home Assistant's local time.

## Entities

| Entity | Purpose |
|---|---|
| `binary_sensor.<name>_grid` | Grid present (device class `power`). The `source` attribute shows what decided: `voltage`, `hold` or `fallback`. |
| `sensor.<name>_shed_loads` | How many loads are off because of the outage. The `entities` attribute lists them. |
| `number.<name>_restore_delay` | Seconds the grid must be back before restoring (default 60). |
| `button.<name>_restore_now` | Restore immediately. |
| `switch.<load>_shed_on_grid_loss` | Per-load flag. |
| `switch.<load>_run_on_schedule` | Only for loads with a window. The window is in the `window_start`/`window_end` attributes. |

## Services and events

- `grid_load_shedding.forget` (`entity_id`): drop switches from the remembered list so they aren't
  turned back on. Use it when something outside the integration legitimately switches a load off during an
  outage. Schedule windows already do this themselves.
- `grid_load_shedding.restore_now`: same as the button.
- Events `grid_load_shedding_shed` and `grid_load_shedding_restored` (`entity_id`: list), for
  notifications.

## Installation

HACS → Integrations → ⋮ → Custom repositories → add this repository (category *Integration*) →
install → restart → Settings → Devices & services → Add integration → *Grid Load Shedding*.

Requires Home Assistant 2025.3 or newer (config subentries).

## Development

```sh
uv sync
uv run pytest
```

Tests run against the Home Assistant version pinned in `pyproject.toml` via
`pytest-homeassistant-custom-component`.
