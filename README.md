# pywiz

Local control for the WiZ bulbs at home. A FastAPI app that listens to a Zigbee button over MQTT
and drives the bulbs directly over the LAN with [pywizlight](https://github.com/sbidy/pywizlight) —
no WiZ cloud involved.

Two ways in:

- **MQTT** — a zigbee2mqtt button publishes an action, the app maps it to a command.
- **HTTP** — the same commands, plus endpoints to inspect, capture and reset scenes.

## Running

```bash
pip install -r requirements.txt
```

```bash
uvicorn pywiz:app --host 0.0.0.0 --port 7007
```

`MQTT_USERNAME` / `MQTT_PASSWORD` are read from the environment. The broker (`192.168.1.105`),
the topic (`zigbee2mqtt/button`) and the bulb IPs are hardcoded in `pywiz.py` — the bulbs need
static leases for this to keep working.

Running `python pywiz.py` directly just toggles the living room and exits; it is a smoke test,
not the app.

## Rooms and bulbs

| room | bulbs |
|---|---|
| `living` | `kitchen`, `living`, `lamp_big`, `lamp_small` |
| `bedroom` | `bedroom` |

`GET /room` lists them with their IPs and scene slugs.

## Scenes

A scene is a room slug plus a scene slug, and holds one `BulbState` per bulb in that room:

```json
{ "state": true, "scene": 29, "brightness": 117 }
```

`brightness` is 0–255 (pywizlight's scale, not the bulb's 0–100 dimming). A state is either
a `scene` id (a WiZ app mode such as Cozy or Candle), a `colortemp`, or a mix of
`rgb` / `warm_white` / `cold_white` — capture picks whichever the bulb actually reports.

The defaults live in `DEFAULT_SCENES` in `pywiz.py`:

| scene | what it is |
|---|---|
| `living/night-tv` | ceiling lights off, both lamps low and warm |
| `living/cooking` | kitchen and small lamp cold and bright, rest warm |
| `living/guests` | everything warm, small lamp off |
| `bedroom/bedtime` | Cozy scene at ~35% |
| `bedroom/dim-light` | warm white at ~50% |

Capturing a scene writes it to `scenes.json` (next to `pywiz.py`, git-ignored) and that captured
version wins from then on, for both the HTTP routes and the buttons. `/default` deletes the
capture and falls back to the code. `scenes.json` only ever holds captures, never the defaults.

### Capturing an app mode

Dynamic WiZ modes (Candle, Cozy, Ocean…) report **only** a scene id and a dimming level — the bulb
generates the color itself, so there is no rgb value to read while one is running. Capture stores
the scene id, which reproduces the mode exactly on replay. Set the mode in the WiZ app, then:

```bash
curl "http://localhost:7007/room/living/scenes/night-tv/capture"
```

Scene ids pywizlight doesn't know about are sent as a raw `setPilot`, so newer app modes still work.

Capture also accepts a slug that has no default, which creates a new scene (say `candle`) —
put it on a button from the ui or with `POST /room/living/scenes/candle/button`.

## Buttons

A `zigbee2mqtt/button` payload (`{"action": "single_button_1"}`) is resolved through the bindings.
A button action does one of three things: run a `WizCommand` (the toggles and brightness steps),
apply a scene, or nothing. `DEFAULT_BINDINGS` in `pywiz.py` holds the layout:

| | single | double | triple | long |
|---|---|---|---|---|
| button 1 | living toggle | `living/cooking` | — | living brighter |
| button 2 | bedroom toggle | — | — | bedroom brighter |
| button 3 | `living/night-tv` | `living/guests` | — | living dimmer |
| button 4 | `bedroom/bedtime` | `bedroom/dim-light` | — | bedroom dimmer |

Rebinding works like capturing: changes go to `bindings.json` (git-ignored) and win over the
defaults, `GET /buttons/{action}/default` puts one back. A scene lives on at most one button, so
moving it clears the old one, and assigning it to a button that was doing something else takes that
button over until you reset it. Anything that ends up matching the default again is dropped from
the file rather than stored, so `bindings.json` only ever holds real differences.

Toggle uses the room as a whole: the living room turns off if *any* of its bulbs is on.
Brightness steps are clamped to 26–255.

## UI

`GET /` serves `ui.html`: every room with its scenes, what each bulb does in them, and a
capture / apply / reset button per scene. The point is the phone — set a mode in the WiZ app,
switch to `http://<host>:7007/`, hit capture. The text box at the bottom of a room captures into
a new scene, slugifying whatever you type ("Candle Test" → `candle-test`).

Each scene also has a dropdown of the sixteen button actions — pick one and the scene moves onto
that button, with each option showing what it currently runs so you can see what you are taking
over. Next to it, a **reset button** appears whenever the scene sits on a button you have changed,
which undoes exactly that — handy right after putting a scene somewhere by mistake. At the bottom,
a grid of all four buttons shows what every press does, with the same reset next to anything
customised.

Capture, reset and reset-button all ask for confirmation first, naming what they are about to
throw away; apply and moving a scene onto a button just happen. Reset is greyed out while a scene
is still the default.

It is one static file with no build step and no dependencies, served straight off disk, so
editing it and reloading the page is enough.

## HTTP API

| route | |
|---|---|
| `GET /` | the ui |
| `GET /scene-names` | WiZ scene id → name, used by the ui |
| `GET /room` | rooms, their bulbs and scene slugs |
| `GET /room/{room}/scenes` | scenes in a room, each marked `default` or `captured` |
| `GET /room/{room}/scenes/{scene}` | the resolved scene definition |
| `GET /room/{room}/scenes/{scene}/apply` | run the scene |
| `GET /room/{room}/scenes/{scene}/capture` | snapshot the room's bulbs into the scene |
| `GET /room/{room}/scenes/{scene}/default` | drop the capture, restore the hardcoded default |
| `POST /room/{room}/scenes/{scene}/button` | put the scene on a button, `{"action": "triple_button_1"}` or `{"action": null}` |
| `GET /buttons` | every button action and what it runs |
| `GET /buttons/{action}/default` | restore a button's default binding |
| `POST /action` | run a button action, e.g. `{"action": "single_button_1"}` |
| `GET` / `POST /brightness_step` | read or set the brightness step (default 75, resets on restart) |
| `GET /debug` | toggle bulb-state logging before and after each command |

