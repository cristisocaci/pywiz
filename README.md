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

## Deploying

It runs in CT 105 (`192.168.1.105`, the Alpine zigbee2mqtt container) as the OpenRC service
`pywiz`, from a clone of this repo at `/root/pywiz` with its own venv at `/root/pywiz/venv`.
Deploying is push to `main`, then run `/root/update_app.sh` inside the container:

```bash
git push
```

```bash
pct exec 105 -- /root/update_app.sh
```

The script does `git pull origin main`, reinstalls `requirements.txt` into the venv and runs
`rc-service pywiz restart`. It only lives on the server; this is what it does:

```sh
#!/bin/sh
set -e

APP_DIR="/root/pywiz"
cd "$APP_DIR"
git pull origin main

. "$APP_DIR/venv/bin/activate"
python -m ensurepip --upgrade
python -m pip install --no-cache-dir -r requirements.txt

rc-service pywiz restart
echo "✔ Code updated"
```

`scenes.json`, `bindings.json` and `timings.db` are git-ignored, so a pull never touches
captured scenes, rebound buttons or the press history. Check it came back with `curl -s http://192.168.1.105:7007/room`.

## Rooms and bulbs

| room | bulbs |
|---|---|
| `living` | `kitchen`, `living`, `lamp_big`, `lamp_small` |
| `bedroom` | `bedroom` |

`GET /room` lists them with their IPs and scene slugs.

## Scenes

A scene is a room slug plus a scene slug, and holds one `BulbState` per bulb in that room:

```json
{ "state": true, "scene": 29, "brightness": 117, "speed": 100 }
```

`brightness` is 0–255 (pywizlight's scale, not the bulb's 0–100 dimming). A state is either
a `scene` id (a WiZ app mode such as Cozy or Candle, with its `speed` if the bulb reports one),
a `colortemp`, or a mix of `rgb` / `warm_white` / `cold_white` — capture picks whichever the bulb
actually reports.

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

Dynamic WiZ modes (Candle, Cozy, Ocean…) report **only** a scene id, a dimming level and sometimes
a speed — the bulb generates the color itself, so there is no rgb value to read while one is
running. Capture stores the scene id, which reproduces the mode exactly on replay. Note that
`dimming` caps the mode rather than describing how bright it looks: Candlelight at 100% is still a
low warm flicker, so a captured `100%` that looks dim is correct, not a lost brightness value.
Set the mode in the WiZ app, then:

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

Toggle uses the room as a whole: the living room turns off if *any* of its bulbs is on. To know
that it asks all four at once, and a bulb that has sat idle for hours can take seconds to answer,
so it does not wait for everyone: the first "on" settles it, otherwise whoever answered within
300 ms (`STATE_CHECK_DEADLINE`) decides, or the first answer if nobody did. Then the command goes
to all four straight away, and a slow bulb switches as soon as it wakes. The price: if the only
bulb that is on is also the one too slow to answer, the room counts as off and the press turns
everything on — press again.

Brightness steps are clamped to 26–255.

## Bulbs that do not answer

A bulb cut from power (switched off at the wall, or off the network) never replies, and pywizlight
raises. Commands and triggers run the bulbs together and survive that: the ones that answer still
get driven, the silent ones are logged and returned as `unreachable`, which the ui shows as
"… (no answer from kitchen)". Capture is the exception — it refuses with a 504 rather than write a
scene that is quietly missing a bulb.

## Why was that press slow?

Slow presses come and go, so every press is timed and kept **for good** in `timings.db`, a
SQLite file next to `pywiz.py` — nothing is ever trimmed (a press is 1–2 KB, so even 30 a day is
~20 MB a year). The ui's **recent presses** pages through all of it, 20 at a time, newest first and
grouped under day headings, with an "only slow ones" filter — tap a press to see where its time went. Right after a slow
press, open the ui on your phone, or:

```bash
curl -s "http://192.168.1.105:7007/timings?slower_than=1000&limit=10"
```

which returns `{"total": …, "offset": …, "limit": …, "presses": [...]}`; add `&offset=10` for
the next page. To dig through it on the server instead:

```bash
pct exec 105 -- sqlite3 /root/pywiz/timings.db "SELECT at, total_ms, json_extract(record, '$.verdict') FROM presses WHERE total_ms > 1000 ORDER BY id DESC LIMIT 20"
```

(`apk add sqlite` first if the CLI is missing; the app only needs Python's built-in `sqlite3`.)
The history used to be a capped `timings.jsonl`; on its first start the database version imports
that file and renames it `timings.jsonl.imported`.

### The path a press takes

```
button ──zigbee──▶ CC2531 ─▶ zigbee2mqtt ─┐
                                          ├─MQTT─▶ pywiz ─UDP─▶ bulbs
clap ─────wifi────────────────────────────┘
```

| step | field | how it is measured |
|---|---|---|
| clap pattern | `pattern_ms` | clap-clap: first clap → pattern recognised. This is just how long your claps took, not lag |
| button deciding single/double | — | **not measurable.** The button waits after a release to see if a second press follows; up to ~1 s, inside the device |
| sender → pywiz | `upstream_ms` | clap: board's NTP clock at publish → pywiz receives it. Button: zigbee2mqtt's `last_seen` (when z2m heard the button) → pywiz receives it. Needs z2m set up, see below |
| radio | `linkquality` | the button's Zigbee signal as z2m reports it, 0–255; low values mean retries on the radio |
| wait for event loop | `queue_ms` | MQTT thread hands the press over → it starts running. Should be ~0 |
| each bulb request | `requests[]` | every `getPilot` / `setPilot`: when it started (`at_ms`, from the start of the press), how long it took (`ms`), how many datagrams pywizlight had sent by then (`datagrams`), and `cancelled` when the toggle stopped waiting for that bulb's state (shown as "stopped waiting" in the ui — it cost the press nothing) |
| total | `total_ms` | pywiz received the press → last bulb answered. Does **not** include `upstream_ms` |

`verdict` is a one-line guess at the culprit, in this order: a bulb that never answered → a bulb
that needed retransmits → a slow sender → a busy event loop → otherwise just the slowest step.

### Reading a record

```json
{
  "at": "2026-10-09T23:41:07.512", "source": "zigbee", "action": "single_button_1",
  "label": "living toggle", "upstream_ms": 31, "linkquality": 87, "queue_ms": 0,
  "requests": [
    {"bulb": "kitchen",    "method": "getPilot", "at_ms": 0,   "ms": 18,   "datagrams": 1},
    {"bulb": "lamp_small", "method": "getPilot", "at_ms": 1,   "ms": 300,  "datagrams": 3, "cancelled": true},
    {"bulb": "kitchen",    "method": "setPilot", "at_ms": 301, "ms": 21,   "datagrams": 1},
    {"bulb": "lamp_small", "method": "setPilot", "at_ms": 301, "ms": 1340, "datagrams": 5}
  ],
  "unreachable": [], "total_ms": 1690,
  "verdict": "lamp_small missed 4 packet(s), its setPilot took 1340ms"
}
```

Read it left to right along the path: the button reached pywiz in 31 ms with a fine signal and the
press started at once. The toggle asked every bulb if it was on; kitchen said "off" in 18 ms,
`lamp_small` said nothing, so after 300 ms the toggle stopped waiting and sent everyone the
command (`at_ms` 301). Kitchen was on 21 ms later; `lamp_small` missed four more packets and
only came on 1.3 s after that. That is the bulb's Wi-Fi, not the button or MQTT.

What the patterns usually mean:

- **`datagrams` > 1 on a bulb** — it missed packets; the bulb's Wi-Fi was asleep or the signal is
  weak. Usually the first press after the lights sat idle. `getPilot` costs come before any
  `setPilot` can go out on toggles and brightness steps.
- **an `error` / `unreachable` bulb** — `no answer within 4s`: off at the wall, or off the network.
- **high `upstream_ms`** — the delay is before pywiz: z2m or the radio for the button (check
  `linkquality`), Wi-Fi or MQTT for clap-clap. Small negative or off-by-tens values are just the
  two clocks disagreeing.
- **fast `total_ms` but it still felt slow** — the time went where nothing can see it: the
  button's single/double wait, or the bulb's own fade-in.
- **a clap that never shows up** — the board's telnet log says `MQTT publish FAILED` when it
  dropped the press because MQTT was down.

The same steps also go to the uvicorn log, one timestamped line each, tagged with the press:

```
23:41:07.512 [single_button_1] zigbee press, executing living toggle (started 0ms after receipt, 31ms from sender)
23:41:09.152 [single_button_1] lamp_small getPilot 1640ms, ~5 datagram(s)
23:41:09.202 [single_button_1] done in 1690ms - lamp_small missed 4 packet(s), its getPilot took 1640ms
```

### zigbee2mqtt: turn on `last_seen`

Button presses only get `upstream_ms` when z2m stamps its messages. In the z2m frontend
(`http://192.168.1.105:8080`) → Settings → Settings → Advanced → **Last seen**, pick `epoch`
(or `ISO_8601`, both work), or in z2m's `configuration.yaml`:

```yaml
advanced:
  last_seen: epoch
```

While you are in there, check the button's own settings page has no **debounce** set — that
holds every press back on purpose.

### Keeping the bulbs awake

The history showed what makes a press slow: the first press after the lights sat untouched for
hours. Warm, a bulb answers in 10–200 ms; after hours idle, some bulbs ignored packets for 1–4 s
(and once a bulb gave no answer at all within 4 s) while others in the same room answered at once
— the bulbs' own Wi-Fi power saving, not the network or pywiz. So pywiz sends every bulb a
`getPilot` every 60 s (`KEEPALIVE_INTERVAL`) in the background. It goes out on its own socket,
never through pywizlight, so a press never queues behind a keepalive waiting on a sleepy bulb.
Only the slow ones are logged, under `[keepalive]`:

```
08:14:02.117 [keepalive] slow to answer: kitchen 1210ms, living no answer
```

Lots of those mean the keepalive is not keeping that bulb warm; none, plus slow presses still in
the history, would point somewhere else.

### Retries

pywizlight on its own resends an unanswered datagram at 0.75 s, 2.25 s, 5.25 s… and waits 13 s.
pywiz overrides that at the top of `pywiz.py` to resend at 0.2 s, 0.6 s, 1.1 s and every 0.5 s
after, giving up after 4 s. `datagrams` is worked out from that schedule and the time the answer
took, so it is the number sent by then, not a count off the wire.

## pywizlight notes

Things in pywizlight 0.6.3 that cost time or hide what happened, and what pywiz does about them:

- **Slow retry schedule** (`bulb.py`, `_send_udp_message_with_retry`): see Retries above. The
  constants are module globals read at call time, which is why overriding them works.
- **One request per bulb at a time.** `wizlight.send` holds a per-bulb `asyncio.Lock` for the
  whole request, retries included, so a second request to the same bulb waits until the first is
  answered or times out. That is why the keepalive does not go through `wizlight`, and why the
  toggle cancels the state checks it stops waiting for — cancelling frees the lock, so that bulb's
  `setPilot` goes out at once instead of after the `getPilot` gives up.
- **Timeouts look like connection errors.** `send` catches `OSError` before `TimeoutError`, and
  since Python 3.11 a timeout *is* an `OSError`, so a bulb that never answered raises
  `WizLightConnectionError('')`. pywiz logs that as `no answer within 4s`.
- **No state cache without push.** `updateState` only reuses a known state when push updates
  (`start_push`) are running, so every toggle and brightness step reads the bulb first. Push would
  remove that read; it is not used, the state check deadline covers the slow case.
- **Answers are matched by method, not request id**, so a late answer to an earlier retransmitted
  request of the same method can be taken as the answer to a new one. Harmless for pywiz: the
  state it carries is still current.
- **`wizlight()` needs an event loop at construction** (`asyncio.get_event_loop()` in
  `__init__`). Fine on the server; on Python 3.14 that raises when no loop is running, so
  `import pywiz` fails there — set a loop first when testing locally.

## UI

`GET /` serves `ui.html`: every room with its scenes, what each bulb does in them, and a
capture / trigger / reset button per scene. The point is the phone — set a mode in the WiZ app,
switch to `http://<host>:7007/`, hit capture. The text box at the bottom of a room captures into
a new scene, slugifying whatever you type ("Candle Test" → `candle-test`).

Every section — each room, the buttons, recent presses — folds away by tapping its title, and the
ui remembers which ones you closed on that phone.

Each scene also has a dropdown of the sixteen button actions — pick one and the scene moves onto
that button, with each option showing what it currently runs so you can see what you are taking
over. Next to it, a **reset button** appears whenever the scene sits on a button you have changed,
which undoes exactly that — handy right after putting a scene somewhere by mistake. At the bottom,
a card per button lists its four presses and what each one does, with the same reset next to
anything customised.

Every press has a **▶** that fires that action through the same path a real press takes, so a
binding can be tried without reaching for the remote.

Capture, reset, reset-button and delete all ask for confirmation first, naming what they are about
to throw away; trigger and moving a scene onto a button just happen. Reset is greyed out while a
scene is still the default, and scenes with no default show **delete** in its place.

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
| `GET /room/{room}/scenes/{scene}/trigger` | run the scene |
| `GET /room/{room}/scenes/{scene}/capture` | snapshot the room's bulbs into the scene |
| `GET /room/{room}/scenes/{scene}/default` | drop the capture, restore the hardcoded default |
| `DELETE /room/{room}/scenes/{scene}` | delete a captured scene that has no default |
| `POST /room/{room}/scenes/{scene}/button` | put the scene on a button, `{"action": "triple_button_1"}` or `{"action": null}` |
| `GET /buttons` | every button action and what it runs |
| `GET /buttons/{action}/default` | restore a button's default binding |
| `POST /action` | run a button action, e.g. `{"action": "single_button_1"}` |
| `GET` / `POST /brightness_step` | read or set the brightness step (default 75, resets on restart) |
| `GET /timings?limit=20&offset=0&slower_than=0` | a page of the press history, newest first, with where the time went: `{total, offset, limit, presses}` (`slower_than` in ms, `limit` up to 500) |
| `GET /debug` | toggle bulb-state logging before and after each command |

