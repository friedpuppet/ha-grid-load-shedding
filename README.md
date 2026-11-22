# Grid Load Shedding

> Personal project, published only so it can be installed through the Home Assistant Marketplace (formerly HACS) as a custom repository.
> Use it if it's useful to you, but there's no support and no promise of stability.

A Home Assistant integration for homes with a hybrid inverter or UPS. It detects when utility-grid
power is lost, turns off selected heavy loads (kettle, washing machine, air conditioners…) so they
don't drain the battery, and turns the same ones back on once the grid has been back for a while.

## How it decides "grid is present"

"Grid" means **the flat runs from the grid**: the inverter is in bypass, not on its battery.

- **Voltage sensor**: the inverter's grid-input voltage. At or below the threshold (typically the
  inverter's grid low-voltage protection) the inverter runs from its battery: Grid goes off at once.
- **Bypass sensor** (optional, recommended): a binary sensor that is on while the inverter actually runs
  from the grid, e.g. its grid relay. With it, Grid is on only while the voltage is above the threshold
  **and** the bypass sensor is on, so loads come back only once the inverter has switched over, not as
  soon as the voltage returns (inverters wait a while before reconnecting). The bypass sensor going off
  also turns Grid off.
- **Short dropouts**: inverter Wi-Fi bridges drop out now and then. While the inverter is `unavailable`,
  the last state is held for *hold time* (default 30 s). Nothing is switched on from a held state.
- **Can't see the inverter**: after the hold time, Grid becomes `unavailable` and no load is touched.
  If the outage turns out to have started meanwhile (on → unavailable → off), the loads are shed then.
- **Fallback** (optional, not recommended): a binary sensor that decides after the hold time instead of
  `unavailable`. Don't use one that depends on the same network as the inverter (e.g. a ping): when Home
  Assistant loses that network, the fallback says "off" and the loads are shed while the grid is fine.

### City grid (informational)

A second sensor, **City grid**, shows whether there is voltage at the supply at all, whether or not the
inverter accepts it: during a brownout it is on while Grid is off. It is on while an optional supply meter
(before any voltage relay and the inverter) or the inverter reads above *City grid present above*
(default 100 V). A meter reading older than *Meter reading valid for* (default 60 s) counts as none,
because a mains-powered meter that lost power keeps its last state for minutes. It never sheds or
restores loads.

## What it does with loads

Add loads under the integration's entry with **Add load**: pick any `switch`. All per-load settings live
in the load's dialog (**Add load** / **Reconfigure**): **Shed on grid loss** (default on), the optional
schedule window, and **Run on schedule**. The two flags also appear as switches on the load's own device.
Dialog and switches edit the same setting, so they are always in sync.

- **Grid goes from on to off**: every load whose flag is on and which is currently on gets turned off and
  remembered. Loads that were already off stay off and are not remembered.
- **Grid has been back for *Restore delay* seconds**: the remembered loads are turned back on.
  If the grid drops again during the delay, the restore is cancelled.
- **Restarts**: the remembered list survives Home Assistant restarts and entry reloads.
- **Unreliable switches** (e.g. Zigbee plugs that drop a command, or lose power behind an inverter): each
  switch gets its own command, retried after 2 s and 5 s, then every 60 s until it succeeds. A command for an
  unavailable switch is sent as soon as it is back. A remembered load leaves the list only once it is really on
  again. A flagged load that comes back from unavailable already on during an outage is turned off and
  remembered too; a load you turn on by hand during the outage is left alone.

### Schedule window (optional, per load)

A load can also get a schedule window, e.g. a boiler that heats 01:00–07:00. Set it when adding the
load or later with **Reconfigure**; clear both times to remove it. A load with a window also gets a
**Run on schedule** switch, on by default. While **Run on schedule** is on:

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

## Sounds and the battery (moved out in 0.12.0)

Sound notifications and the battery analysis used to live here. Since 0.12.0 they are separate integrations:
[Power Sounds](https://github.com/friedpuppet/ha-power-sounds) (grid lost/restored and battery level sounds; give it
this integration's Grid sensor) and [Battery Monitor](https://github.com/friedpuppet/ha-battery-monitor) (battery
state, sessions, time left, time to full). Updating to 0.12.0 removes the old sound notifications, the battery options
and the battery sensors from the entry; set them up again in those integrations.

## Entities

| Entity | Purpose |
|---|---|
| `binary_sensor.<name>_grid` | Flat on grid power, the inverter in bypass (device class `power`). The `source` attribute shows what decided: `voltage`, `relay` (the bypass sensor), `hold` or `fallback`. |
| `binary_sensor.<name>_city_grid` | Voltage at the supply (informational). Attributes `source` (`meter` or `inverter`) and `voltage`. |
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

Settings → Marketplace (HACS before HA 2026.11) → ⋮ → Custom repositories → add this repository (category
*Integration*) → install → restart → Settings → Devices & services → Add integration → *Grid Load Shedding*.

Requires Home Assistant 2025.3 or newer (config subentries).

## Development

```sh
uv sync
uv run pytest
```

Tests run against the Home Assistant version pinned in `pyproject.toml` via
`pytest-homeassistant-custom-component`.
