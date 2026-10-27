# Octopus Happy Hour

A [pyscript](https://github.com/custom-components/pyscript) app for Home Assistant that makes the most of **Octopus Octoplus Happy Hours** (and other Power Up / free electricity sessions). While the electricity is free it:

- charges your **Solis** battery from the grid at full speed,
- boosts your **Cosy** hot water to 50 °C,
- runs anything else you want to use the free power (car charger, dishwasher, heating boost, …),
- and sends you a push notification.

It's a companion to [Solis EV-Follow](https://github.com/darvds/SolisOctopusEVFollow), [Solis Power Down](https://github.com/darvds/SolisOctopusPowerDown) and [Cosy WC Tuner](https://github.com/darvds/OctopusCosyWCTuner), and runs in the same `pyscript:` block.

## How it works

1. **Find.** It watches the Octopus integration's power up events entity. Every booked session becomes a happy hour. Back-to-back or overlapping sessions are merged into one window.
2. **Book (optional).** With `auto_join: true` it books an available Weekend Happy Hour (the earliest, or the latest) whenever you have a token left.
3. **Announce.** When a new happy hour is booked (by the app, or by you in the Octopus app) you get a notification.
4. **Arm the battery.** `prepare_minutes` before the start it writes one Solis **charge slot**: the happy hour's time window, `charge_current` and `charge_soc`, then switches the slot on.
5. **Start.** At the start time (checked every minute):
   - the hot water is boosted to `hot_water_temperature` until the end of the happy hour (`octopus_energy.boost_water_heater`),
   - your `start_actions` run,
   - you get a "Happy Hour started" notification.
6. **End.** It switches the charge slot off and resets it to `00:00-00:00`, runs your `end_actions` and sends a "Happy Hour over" notification.

**Fail-safe:** the inverter runs the charge slot by itself and the Cosy ends the boost by itself. If Home Assistant, pyscript or SolisCloud goes down, charging and boosting still stop at the end of the happy hour.

### Example

You book a Saturday 14:00–15:00 happy hour in the Octopus app:

| Time | What happens |
|---|---|
| 11:20 | "Happy Hour booked: Sat 31 Oct 14:00-15:00. Battery charging to 100%, hot water boosting to 50°C." |
| 13:55 | Charge slot 3 is set to `14:00-15:00`, 100 A, 100% and switched on |
| 14:00 | The inverter starts charging. The hot water is boosted for 1 h at 50 °C, start actions run, and you get "Happy Hour started: free electricity until 15:00." |
| 15:00 | Charge slot 3 is switched off and cleared, end actions run, and you get "Happy Hour over" |

## Requirements

- Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS).
- [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy), enrolled in Octoplus, with the **power up events** entity (`event.octopus_energy_<account>_octoplus_power_up_events`) **enabled**. It's disabled by default: enable it in Settings → Devices & services → Entities.
- For battery charging, a Solis hybrid inverter:
  - **6-slot firmware** (HMI ≥ `4B00`)
  - **time-of-use / timed charge-discharge enabled**
  - SolisCloud API access (key ID and secret)
- For hot water, an Octopus Cosy showing as `water_heater.octopus_energy_heat_pump_<id>`.

You can use any of the parts on its own. Without SolisCloud credentials the battery part is off; without `heat_pump_id` the hot water part is off.

## Installation

1. Copy `pyscript/apps/octopus_happy_hour.py` to `<config>/pyscript/apps/octopus_happy_hour.py`.
2. Merge the block from [`configuration.example.yaml`](configuration.example.yaml) into `configuration.yaml`. Keep a single `pyscript:` block.
3. Use the same SolisCloud secrets as EV-Follow (`solis_key_id`, `solis_secret`, `solis_username`, `solis_password`, `solis_plant_id`), and the same `heat_pump_id` as the Cosy WC Tuner.
4. Pick a free `charge_slot`. EV-Follow uses slots 1 and 2, so the default is 3. If you run EV-Follow with `clear_other_slots: true`, it clears slot 3 when it starts up. This app waits `startup_delay_seconds` (120 s) and then rewrites the slot, so a restart during a happy hour still works. Setting `clear_other_slots: false` in EV-Follow avoids the clear altogether.
5. Restart Home Assistant.

Start with `diagnostics_only: true`. Run **`pyscript.octopus_happy_hour_preview`** and check the log and `sensor.octopus_happy_hour`. In diagnostics mode the app logs the inverter writes, the boost and your actions instead of doing them, but it still sends notifications. When it looks right, set `diagnostics_only: false` and restart.

## Configuration

| Option | Default | Description |
|---|---|---|
| `power_up_events_entity` | **required** | `event.octopus_energy_<account>_octoplus_power_up_events` |
| `auto_join` | `false` | Book available Weekend Happy Hours while you have tokens |
| `join_selection` | `earliest` | Which available happy hour to book: `earliest` or `latest` |
| `weekend_happy_hours_entity` | derived | Token count sensor. It's derived from `power_up_events_entity`. |
| `notify_service` | none | e.g. `notify.mobile_app_phone`, or a list of several |
| `notify_data` | none | Extra `data` for the notification, e.g. `push: {interruption-level: time-sensitive}` |
| `notify_scheduled` | `true` | Notify when a new happy hour is booked |
| `notify_end` | `true` | Notify when a happy hour finishes |
| `key_id`, `secret`, `username`, `password`, `plantId` | none | SolisCloud API credentials. Needed for battery charging. |
| `inverter_sn` / `inverter_id` | auto | Only needed if the plant has more than one storage inverter |
| `battery_charge` | `true` with credentials | Charge the battery during happy hours |
| `charge_slot` | `3` | Solis charge slot driven by this app (1–6) |
| `charge_current` | `100` | Charge current (A). The inverter caps it at its own maximum. |
| `charge_soc` | `100` | SOC to charge up to |
| `prepare_minutes` | `5` | How long before the start the slot is written |
| `heat_pump_id` | none | The id in your heat pump entity names |
| `water_heater_entity` | from `heat_pump_id` | Override the water heater entity |
| `hot_water_boost` | `true` with a water heater | Boost the hot water during happy hours |
| `hot_water_temperature` | `50` | Boost target (°C, 40–60) |
| `start_actions` | none | HA actions to run when a happy hour starts (see below) |
| `end_actions` | none | HA actions to run when it ends |
| `inverter_timezone` | HA time zone | Time zone the inverter clock and slots use |
| `sync_inverter_time` | `true` | Sync the inverter clock (at most once per 24h) |
| `diagnostics_only` | `false` | Log instead of writing to the inverter, boosting or running actions |
| `control_retries` | `3` | Retries per SolisCloud write |
| `verify_readback` | `false` | Read each value back after writing |
| `startup_delay_seconds` | `120` | Wait for other integrations (and EV-Follow) after startup |

### Other things to run

`start_actions` and `end_actions` take any Home Assistant action, so you can add whatever should soak up the free power:

```yaml
start_actions:
  - service: switch.turn_on            # start the car charging
    target:
      entity_id: switch.hypervolt_charging
  - service: script.turn_on            # a script that starts the dishwasher
    target:
      entity_id: script.run_dishwasher
  - service: octopus_energy.boost_heat_pump_zone   # pre-heat the house
    target:
      entity_id: climate.octopus_energy_heat_pump_<id>_zone_1
    data: {hours: 1, minutes: 0, target_temperature: 22}
end_actions:
  - service: switch.turn_off
    target:
      entity_id: switch.hypervolt_charging
```

Each entry needs `service` (or `action`) as `<domain>.<name>`, with optional `data` and `target`. A plain string like `script.dishwasher_eco` also works. If an action fails, it's logged and the rest still run.

For anything that needs more logic, trigger your own automations on `binary_sensor.octopus_happy_hour_active`.

## Monitoring

- **`sensor.octopus_happy_hour`**
  - **State:** one of the following.
    - `idle`: no happy hour booked.
    - `scheduled`: a happy hour is booked but the charge slot isn't written yet.
    - `armed`: the charge slot is written and the start is a few minutes away.
    - `active`: the happy hour is running.
  - **Attributes:**
    - `window`, `start`, `end` and `minutes_to_start` for the next or current happy hour
    - `booked`: every upcoming happy hour, and `tokens`: Weekend Happy Hour tokens left
    - what's programmed on the inverter (`programmed_window`, `programmed_current`, `programmed_soc`, `slot_enabled`)
    - `boost_until`
  - **`power_up_events`:** `ok`, `missing` or `not_configured`. When it isn't `ok`, a `warning` attribute explains why. The same message is logged as an error once an hour.
- **`binary_sensor.octopus_happy_hour_active`**: `on` during a happy hour.
- **Actions** (Developer Tools → Actions):
  - `pyscript.octopus_happy_hour_reconcile`: book (if `auto_join`), re-check and update the charge slot now.
  - `pyscript.octopus_happy_hour_preview`: log the booked and available happy hours and what will happen, without changing anything.
  - `pyscript.octopus_happy_hour_clear`: switch off and clear the charge slot.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      pyscript.octopus_happy_hour: debug
  ```

## Notes

- **Power Ups count too.** The integration lists booked Weekend Happy Hours, Power Ups and free electricity sessions together in the same `events` attribute and doesn't say which is which. The app treats them all as happy hours. All of them reward using more power, so the same actions make sense.
- **Restarts.** After a restart, happy hours that are already booked aren't announced again. If a happy hour is already running, only the charge slot is rewritten. The boost is still running on the Cosy, and the start actions and notification aren't repeated. The end actions and notification still happen.
- **Late starts.** If the happy hour is already under way when it's picked up, the boost only covers the time that's left, rounded to 15 minutes. With less than 15 minutes left there's no boost.
- **Longer windows.** If another session is booked straight after a running happy hour, the window gets longer. The charge slot is rewritten and the boost is topped up.
- **Power Down peak export.** If Power Down's daily peak export window overlaps a happy hour, the inverter has a charge slot and a discharge slot at the same time. Skip that day's peak export with Power Down's `peak_export_switch`.

## Development

The tests use only the Python standard library (3.9+):

```bash
python3 -m unittest discover -s tests -t .
```

`tests/harness.py` loads the app with fakes for:
- pyscript's globals
- the clock
- HA states and services
- SolisCloud

The tests cover:
- **Sessions:** parsing, merging back-to-back sessions, the missing-entity warning
- **Auto-join:** tokens, earliest / latest, full and started sessions, retry back-off
- **The happy hour timeline:** arm, start (boost, actions, notification), end, cancellations, longer windows, late starts and restarts
- **SolisCloud:** charge slot CIDs, write order (values first, switch last) and the `yuanzhi` mask
- **Diagnostics mode** and **triggers / services**

CI runs the suite on every push (`.github/workflows/tests.yml`).

## Credits

- [Moondevil-ha/solis-smart-charging](https://github.com/Moondevil-ha/solis-smart-charging) and [stevegal/solis_control](https://github.com/stevegal/solis_control) (MIT): SolisCloud API signing, login and control writes.
- [mkuthan/solis-cloud-control](https://github.com/mkuthan/solis-cloud-control): charge slot current / SOC / switch CIDs, and the `yuanzhi` bitmask.
- [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy): Octoplus power up entities, the happy hour join action and the water heater boost.

Not affiliated with Solis, Octopus Energy or Home Assistant.

## License

[MIT](LICENSE)
