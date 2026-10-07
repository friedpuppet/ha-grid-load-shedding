# grid-load-shedding — Home Assistant integration

Subproject of `homeassistant` (see `../AGENTS.md`). This is the HA custom integration
**`grid_load_shedding`** ("Grid Load Shedding"). It detects utility-grid loss from a voltage
sensor (with hold and fallback) and sheds/restores flagged switches. It generalizes what used to be the
template helper `binary_sensor.e_elektrika`, the 18 `switch.*_vimikati_bez_merezhi` template switches,
the `power_shed_heavy_loads` automation and the `sensor.vimkneno_cherez_vidkliuchennia` trigger
sensor on this HA instance (see `../electricity.md`).

## Status

- Code and tests are done (`uv run pytest`: all green against HA 2026.8.3). Latest release **v0.4.0** (GitHub release + tag; v0.1.0 had no schedule windows, v0.4.0 added sound notifications). Bump `manifest.json` `version` with each release.
- Repo: **[friedpuppet/ha-grid-load-shedding](https://github.com/friedpuppet/ha-grid-load-shedding)**
  (public), `origin` without credentials. Token: fine-grained PAT, owner `friedpuppet`, this repo only,
  at `~/.config/github/token-grid-load-shedding` (Contents + Workflows RW, Actions RO; **no
  Administration**, so repo settings like topics are done by the user in the UI). The old
  `~/.config/github/token` belongs to the `ha-linux-monitoring` org and can't be reused here: a
  fine-grained PAT has a single resource owner.
- Push without storing the token:
  `git -c http.https://github.com/.extraheader="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$(cat ~/.config/github/token-grid-load-shedding)" | base64 -w0)" push`
- **Personal project, not for the public**: installed only as a HACS *custom repository*. It is
  deliberately **not** submitted to the HACS default store or to home-assistant/brands. The repo is
  public only so HACS can fetch it without auth. Don't add publishing chores (HACS-store
  requirements, brands PRs, support docs).
- CI (`.github/workflows/validate.yml`): hassfest + pytest. The `hacs/action` job was removed because
  it only matters for the HACS default store. The brand icon in
  `custom_components/grid_load_shedding/brand/` (`icon.png` 256², `icon@2x.png` 512²) stays, since HA
  shows it in the UI.
- **Installed on the live HA (2026-10-07, HA 2026.9.4)** via HACS (custom repository, HACS repo id
  `1408580017`), entry «Мережа» `01M4AZ6M70PVSKY7XEM7G7YMC9`, 18 load subentries. The boiler has a
  01:00–07:00 window. The cutover from the old template/automation setup is done (see `PLAN.md` and
  `../electricity.md`).
  - Live entity_ids: Grid is renamed to `binary_sensor.e_elektrika`; Shed loads to
    `sensor.vimkneno_cherez_vidkliuchennia`; `number.merezha_restore_delay`,
    `button.merezha_restore_now`. Per-load switches are `switch.<area>_<plug>_shed_on_grid_loss` /
    `_run_on_schedule`: HA 2026.9 builds entity_ids from the *English* name plus the area.
  - **Updating**: release a new version (bump `manifest.json`), then HACS → update → restart Core.
    Via WS: `hacs/repository/download` with `repository: "1408580017"` and `version: "vX.Y.Z"`.
  - **Sound notifications (v0.4.0, 2026-10-07)**: subentry «VLC-TELNET» on `media_player.vlc_telnet` (VLC add-on
    `core_vlc`, output = analog jack `rk3528-acodec`, a small speaker is plugged in). Sounds
    `media-source://media_source/local/grid_lost.wav` / `grid_restored.wav` (generated chimes in
    `/var/lib/homeassistant/media/`; the user may replace them via Media → Local media), volume 50 % (confirmed
    audible), window 11:00–22:00. Entities: `switch.vitalnia_vlc_telnet_sound_notifications`,
    `button.vitalnia_vlc_telnet_test_sound_grid_{lost,restored}`. VLC has no `MEDIA_ANNOUNCE`; the integration
    calls `volume_set` before each `play_media`. Both test sounds confirmed audible. The speaker is powered from
    an inverter-backed outlet, so it keeps working through an outage (except after the battery is empty and the
    inverter has shut down). Not yet heard on a real outage.
  - Not yet seen a real outage with the integration (the old automation handled 2026-10-07 09:01–12:05 Kyiv).

## Layout

| Path | What |
|---|---|
| `custom_components/grid_load_shedding/grid.py` | `GridMonitor`: grid-presence state machine (voltage → hold timer → fallback). No entities, unit-testable. |
| `.../shedder.py` | `Shedder` + `Load` dataclass: shed on `on → off`, restore after the delay, per-load schedule windows (`async_track_time_change` at start/end), persisted `shed` and `missed` lists (`Store`, key `grid_load_shedding.<entry_id>`). |
| `.../__init__.py` | Wires the monitor and shedder into `runtime_data`, registers the `forget`/`restore_now` services, reloads on options or subentry change. |
| `.../config_flow.py` | Entry flow (voltage/threshold/hold/fallback), options flow, `load` subentry flow. |
| `.../binary_sensor.py` `sensor.py` `number.py` `button.py` | Entities on the entry's own "Grid" service device. |
| `.../switch.py` | Per-load switches on the *load's* device (`entity.device_entry = async_entity_id_to_device(...)`): "Shed on grid loss" for every load, "Run on schedule" for loads with a window. Unique ids `<subentry_id>_shed_on_grid_loss` / `_run_on_schedule`. |
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
- **Schedule windows** (subentry data `window_start`/`window_end`, `HH:MM:SS` local, may cross
  midnight; set on add or via the subentry *reconfigure* step). They act only while "Run on schedule"
  is on:
  - start: turn on if grid, else add to `missed`;
  - end: turn off, drop from `shed`/`missed`;
  - restore: shed windowed loads only inside the window, plus `missed` ones inside the window.

  Manual off inside the window is respected: there is no "enforce state" loop. The user chose this over a
  separate schedule automation to avoid the two fighting. It replaces the old `boiler_schedule`
  automation and `input_boolean.boiler_vikoristovuietsia` (→ "Run on schedule").
- Time-jump gotcha in tests: `async_fire_time_changed` after a big jump fires stale
  `async_track_time_change` triggers late (see `test_window_crossing_midnight`). Real time doesn't jump,
  so don't "fix" it in the code.
- **Per-load flags** (`shed_on_grid_loss`, `run_on_schedule`, default True) live in **subentry data**,
  the single source of truth. The user wanted every per-load setting in the load's Reconfigure dialog
  *and* as device switches, kept in sync.
  - The switches write via `async_update_subentry`.
  - The update listener compares `_signature(entry)` (options + switch/window per subentry). If only flags
    changed, it applies them live (`_apply_flags` + `shedder.async_notify()`) **without a reload**, so
    restore timers survive a dashboard toggle. Anything else reloads.
  - The v0.2.x → v0.3.0 migration: if the key is missing from the subentry, the switch writes its restored
    entity state into it once.
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
