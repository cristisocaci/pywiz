import asyncio

from enum import Enum
from pathlib import Path
from pywizlight import wizlight, PilotBuilder
from pywizlight.bulb import PilotParser
from pywizlight.exceptions import WizLightError, WizLightConnectionError
from pywizlight.scenes import SCENES
from pywizlight.utils import hex_to_percent
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, model_validator
from contextlib import asynccontextmanager
import paho.mqtt.client as mqtt
import os
import json
import time
import pywizlight.bulb as wizlight_bulb
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

# pywizlight resends a lost datagram only at 0.75s, 2.25s, 5.25s, 8.25s and waits 13s for an
# answer, so one dropped packet costs a button press 0.75s and three cost 5s - and a bulb that is
# off at the wall stalls everything waiting on it for 13s. getPilot / setPilot are idempotent,
# so resend early and often, and give up on a silent bulb sooner. Read at call time, so this works.
wizlight_bulb.FIRST_SEND_INTERVAL = 0.2
wizlight_bulb.MAX_BACKOFF = 0.5
wizlight_bulb.MAX_SEND_DATAGRAMS = 12
wizlight_bulb.TIMEOUT = 4

# A toggle has to know if the room is on before it can switch it. After hours idle a bulb can take
# 1-4s to answer, so decide on whoever answered within this, and send the command to everyone.
STATE_CHECK_DEADLINE = 0.3

# How often every bulb gets a getPilot in the background, so it is not cold when a press comes.
KEEPALIVE_INTERVAL = 60
KEEPALIVE_SLOW_MS = 500   # keepalive answers slower than this are logged

# What the current log lines belong to ("single_button_1", "scene living/cooking"), and the
# timing record of the press being run. asyncio copies both into every task, so the bulb calls
# of one press all log under its name and land in its record.
current_action: ContextVar[str] = ContextVar("current_action", default="-")
current_press: ContextVar["Press | None"] = ContextVar("current_press", default=None)

def log(message):
    """Timestamped and flushed - stdout to a file is block-buffered, so plain prints show up late."""
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{stamp} [{current_action.get()}] {message}", flush=True)

def ms_since(start):
    return f"{(time.monotonic() - start) * 1000:.0f}ms"

def datagrams_sent(elapsed):
    """How many datagrams pywizlight had sent when the answer came, worked out from its schedule.
    More than one means the bulb missed a packet and the press paid for a retransmit."""
    sent, at, wait = 1, 0.0, wizlight_bulb.FIRST_SEND_INTERVAL
    while sent < wizlight_bulb.MAX_SEND_DATAGRAMS and at + wait <= elapsed:
        at += wait
        sent += 1
        wait = min(wait * 2, wizlight_bulb.MAX_BACKOFF)
    return sent

class TimedWizlight(wizlight):
    """A wizlight that logs how long every request to the bulb took, and adds it to the press."""

    def __init__(self, name: str, ip: str):
        super().__init__(ip)
        self.name = name

    async def send(self, msg_dict):
        start = time.monotonic()
        method = msg_dict.get("method")
        error = None
        cancelled = False
        try:
            return await super().send(msg_dict)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as e:
            error = describe_bulb_error(e)
            raise
        finally:
            elapsed = time.monotonic() - start
            datagrams = datagrams_sent(elapsed)
            if cancelled:
                log(f"{self.name} {method} stopped waiting after {elapsed * 1000:.0f}ms")
            elif error:
                log(f"{self.name} {method} failed after {elapsed * 1000:.0f}ms: {error}")
            else:
                log(f"{self.name} {method} {elapsed * 1000:.0f}ms, ~{datagrams} datagram(s)")

            press = current_press.get()
            if press is not None:
                press.add_request(self.name, method, start, elapsed, datagrams, error, cancelled)

def describe_bulb_error(e: Exception):
    """pywizlight catches OSError before TimeoutError, and since Python 3.11 a timeout *is* an
    OSError - so a bulb that never answered surfaces as WizLightConnectionError('')."""
    if isinstance(e, WizLightConnectionError) and not str(e):
        return f"no answer within {wizlight_bulb.TIMEOUT}s"
    return repr(e)

@dataclass
class Receipt:
    """When a press reached pywiz, and what the sender said about its own side of the trip."""
    source: str                       # "zigbee", "clap" or "http"
    mono: float = field(default_factory=time.monotonic)
    wall: float = field(default_factory=time.time)
    upstream_ms: int | None = None    # sender's timestamp -> MQTT delivered it here
    pattern_ms: int | None = None     # clap only: first clap -> pattern recognised
    linkquality: int | None = None    # zigbee only: the button's radio signal, 0-255

    @classmethod
    def from_payload(cls, payload: dict):
        receipt = cls(source=payload.get("source", "zigbee"),
                      pattern_ms=payload.get("pattern_ms"),
                      linkquality=payload.get("linkquality"))
        sent = sender_time(payload)
        if sent is not None:
            receipt.upstream_ms = round((receipt.wall - sent) * 1000)
        return receipt

def sender_time(payload: dict):
    """Epoch seconds the sender stamped the press with - clap-clap's `sent_at`, or zigbee2mqtt's
    `last_seen` (when z2m heard the button), which z2m only adds with advanced.last_seen set."""
    if "sent_at" in payload:
        return float(payload["sent_at"])

    last_seen = payload.get("last_seen")
    if isinstance(last_seen, (int, float)):
        return last_seen / 1000
    if isinstance(last_seen, str):
        try:
            return datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None

class Press:
    """The timing record of one press, from the moment it reached pywiz to the last bulb answer."""

    def __init__(self, name: str, receipt: Receipt):
        self.receipt = receipt
        self.started = time.monotonic()
        self.record = {
            "at": datetime.fromtimestamp(receipt.wall).isoformat(timespec="milliseconds"),
            "source": receipt.source,
            "action": name,
            "upstream_ms": receipt.upstream_ms,
            "pattern_ms": receipt.pattern_ms,
            "linkquality": receipt.linkquality,
            "queue_ms": round((self.started - receipt.mono) * 1000),
            "requests": [],
        }

    def add_request(self, bulb, method, start, elapsed, datagrams, error, cancelled=False):
        self.record["requests"].append({
            "bulb": bulb,
            "method": method,
            "at_ms": round((start - self.started) * 1000),
            "ms": round(elapsed * 1000),
            "datagrams": datagrams,
            **({"error": error} if error else {}),
            **({"cancelled": True} if cancelled else {}),
        })

    def finish(self, label: str, unreachable):
        self.record["label"] = label
        self.record["unreachable"] = unreachable
        self.record["total_ms"] = round((time.monotonic() - self.receipt.mono) * 1000)
        self.record["verdict"] = verdict(self.record)
        timings.add(self.record)
        log(f"done in {self.record['total_ms']}ms - {self.record['verdict']}")

def verdict(record):
    """One line on where the time went, so a slow press explains itself in the history."""
    requests = record["requests"]
    if record["unreachable"]:
        return f"no answer from {', '.join(record['unreachable'])}"

    # A request we stopped waiting for (the toggle's state check) did not cost the press anything.
    retried = [r for r in requests if r["datagrams"] > 1 and not r.get("cancelled")]
    if retried:
        worst = max(retried, key=lambda r: r["ms"])
        return (f"{worst['bulb']} missed {worst['datagrams'] - 1} packet(s), "
                f"its {worst['method']} took {worst['ms']}ms")

    if (record["upstream_ms"] or 0) > 300:
        return f"{record['source']} -> pywiz took {record['upstream_ms']}ms"
    if record["queue_ms"] > 100:
        return f"waited {record['queue_ms']}ms for the event loop"
    waited_for = [r for r in requests if not r.get("cancelled")]
    if waited_for:
        slowest = max(waited_for, key=lambda r: r["ms"])
        return f"slowest step {slowest['bulb']} {slowest['method']} {slowest['ms']}ms"
    return "no bulb calls"

class TimingHistory:
    """The last presses with their timings, kept across restarts in timings.jsonl."""

    def __init__(self, path: Path, keep: int = 500):
        self.path = path
        self.keep = keep
        self.records = deque(maxlen=keep)
        self.appended = 0

        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self.records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self._rewrite()

    def _rewrite(self):
        with self.path.open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(record) + "\n" for record in self.records)

    def add(self, record):
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        # Appending is cheap; trim the file back to `keep` lines every `keep` presses.
        self.appended += 1
        if self.appended >= self.keep:
            self._rewrite()
            self.appended = 0

    def recent(self, limit: int, slower_than: int = 0):
        matching = [r for r in reversed(self.records) if r.get("total_ms", 0) >= slower_than]
        return matching[:limit]

SCENES_FILE = Path(__file__).with_name("scenes.json")
BINDINGS_FILE = Path(__file__).with_name("bindings.json")
UI_FILE = Path(__file__).with_name("ui.html")
TIMINGS_FILE = Path(__file__).with_name("timings.jsonl")

class WizCommand(str, Enum):
    """Everything a button can do that is not a scene."""
    BEDROOM_TOGGLE = 'BEDROOM_TOGGLE'
    BEDROOM_BRIGHTNESS_UP = 'BEDROOM_BRIGHTNESS_UP'
    BEDROOM_BRIGHTNESS_DOWN = 'BEDROOM_BRIGHTNESS_DOWN'

    LIVING_TOGGLE = 'LIVING_TOGGLE'
    LIVING_BRIGHTNESS_UP = 'LIVING_BRIGHTNESS_UP'
    LIVING_BRIGHTNESS_DOWN = 'LIVING_BRIGHTNESS_DOWN'

class ButtonAction(str, Enum):
    single_button_1 = "single_button_1"
    double_button_1 = "double_button_1"
    triple_button_1 = "triple_button_1"
    long_button_1 = "long_button_1"
    single_button_2 = "single_button_2"
    double_button_2 = "double_button_2"
    triple_button_2 = "triple_button_2"
    long_button_2 = "long_button_2"
    single_button_3 = "single_button_3"
    double_button_3 = "double_button_3"
    triple_button_3 = "triple_button_3"
    long_button_3 = "long_button_3"
    single_button_4 = "single_button_4"
    double_button_4 = "double_button_4"
    triple_button_4 = "triple_button_4"
    long_button_4 = "long_button_4"

class ActionRequest(BaseModel):
    action: ButtonAction

class BrightnessStepRequest(BaseModel):
    brightness_step: int

class Binding(BaseModel):
    """What a button action does: a command, a scene, or nothing."""
    command: WizCommand | None = None
    room: str | None = None
    scene: str | None = None

    @model_validator(mode="after")
    def check_target(self):
        if (self.room is None) != (self.scene is None):
            raise ValueError("room and scene go together")
        if self.command is not None and self.scene is not None:
            raise ValueError("a button is either a command or a scene, not both")
        return self

    @property
    def is_empty(self):
        return self.command is None and self.scene is None

    @property
    def label(self):
        if self.command is not None:
            return self.command.value.lower().replace("_", " ")
        if self.scene is not None:
            return f"{self.room}/{self.scene}"
        return "unassigned"

class ButtonRequest(BaseModel):
    action: ButtonAction | None = None

class BulbState(BaseModel):
    """What a single bulb should look like as part of a scene."""
    state: bool = True
    scene: int | None = None
    speed: int | None = None
    brightness: int | None = None
    warm_white: int | None = None
    cold_white: int | None = None
    rgb: tuple[int, int, int] | None = None
    colortemp: int | None = None

    @classmethod
    def capture(cls, state: PilotParser):
        if not state.get_state():
            return cls(state=False)

        brightness = state.get_brightness()

        # While an app mode (Candle, Cozy, ...) is running the bulb reports only
        # its sceneId and dimming - no rgb/warm/cold - so the mode is the state.
        scene = state.get_scene_id()
        if scene:
            return cls(scene=scene, brightness=brightness, speed=state.get_speed())

        colortemp = state.get_colortemp()
        if colortemp is not None:
            return cls(brightness=brightness, colortemp=colortemp)

        r, g, b = state.get_rgb()
        return cls(
            brightness=brightness,
            rgb=(r, g, b) if None not in (r, g, b) else None,
            warm_white=state.get_warm_white(),
            cold_white=state.get_cold_white())

    def to_pilot(self):
        return PilotBuilder(
            scene=self.scene,
            speed=self.speed,
            brightness=self.brightness,
            rgb=self.rgb,
            colortemp=self.colortemp,
            warm_white=self.warm_white,
            cold_white=self.cold_white)

DEFAULT_SCENES: dict[str, dict[str, dict[str, BulbState]]] = {
    "living": {
        "night-tv": {
            "kitchen": BulbState(state=False),
            "living": BulbState(state=False),
            "lamp_big": BulbState(warm_white=255, brightness=90),
            "lamp_small": BulbState(warm_white=255, brightness=26),
        },
        "cooking": {
            "kitchen": BulbState(cold_white=255, brightness=255),
            "living": BulbState(warm_white=255, brightness=130),
            "lamp_big": BulbState(warm_white=255, brightness=130),
            "lamp_small": BulbState(cold_white=255, brightness=255),
        },
        "guests": {
            "kitchen": BulbState(warm_white=255, brightness=125),
            "living": BulbState(warm_white=255, brightness=255),
            "lamp_big": BulbState(warm_white=255, brightness=125),
            "lamp_small": BulbState(state=False),
        },
    },
    "bedroom": {
        "bedtime": {
            "bedroom": BulbState(scene=6, brightness=90),  # cozy
        },
        "dim-light": {
            "bedroom": BulbState(warm_white=255, brightness=125),
        },
    },
}

DEFAULT_BINDINGS: dict[ButtonAction, Binding] = {
    ButtonAction.single_button_1: Binding(command=WizCommand.LIVING_TOGGLE),
    ButtonAction.double_button_1: Binding(room="living", scene="cooking"),
    ButtonAction.long_button_1: Binding(command=WizCommand.LIVING_BRIGHTNESS_UP),

    ButtonAction.single_button_2: Binding(command=WizCommand.BEDROOM_TOGGLE),
    ButtonAction.long_button_2: Binding(command=WizCommand.BEDROOM_BRIGHTNESS_UP),

    ButtonAction.single_button_3: Binding(room="living", scene="night-tv"),
    ButtonAction.double_button_3: Binding(room="living", scene="guests"),
    ButtonAction.long_button_3: Binding(command=WizCommand.LIVING_BRIGHTNESS_DOWN),

    ButtonAction.single_button_4: Binding(room="bedroom", scene="bedtime"),
    ButtonAction.double_button_4: Binding(room="bedroom", scene="dim-light"),
    ButtonAction.long_button_4: Binding(command=WizCommand.BEDROOM_BRIGHTNESS_DOWN),
}

def load_bindings():
    """Read the button bindings that override the defaults."""
    if not BINDINGS_FILE.exists():
        return {}

    with BINDINGS_FILE.open(encoding="utf-8") as f:
        raw = json.load(f)

    return {ButtonAction(action): Binding(**binding) for action, binding in raw.items()}

def save_bindings(bindings):
    raw = {action.value: binding.model_dump(exclude_none=True) for action, binding in bindings.items()}

    with BINDINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)

def load_scenes():
    """Read the captured scenes that override the defaults."""
    if not SCENES_FILE.exists():
        return {}

    with SCENES_FILE.open(encoding="utf-8") as f:
        raw = json.load(f)

    return {
        room: {
            slug: {bulb: BulbState(**state) for bulb, state in bulbs.items()}
            for slug, bulbs in scenes.items()
        }
        for room, scenes in raw.items()
    }

def save_scenes(scenes):
    raw = {
        room: {
            slug: {bulb: state.model_dump(exclude_none=True) for bulb, state in bulbs.items()}
            for slug, bulbs in room_scenes.items()
        }
        for room, room_scenes in scenes.items() if room_scenes
    }

    with SCENES_FILE.open("w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)

async def gather_bulbs(names, calls):
    """A bulb that is off at the wall never answers - that must not take the others down."""
    results = await asyncio.gather(*calls, return_exceptions=True)

    unreachable = []
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            unreachable.append(name)
            log(f"Bulb '{name}' did not answer: {result!r}")

    return unreachable

class Wiz:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Wiz, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.debug = False
        self.kitchen_light = TimedWizlight("kitchen", "192.168.1.129")
        self.living_light = TimedWizlight("living", "192.168.1.130")
        self.bedroom_light = TimedWizlight("bedroom", "192.168.1.135")
        self.lamp_big = TimedWizlight("lamp_big", "192.168.1.254")
        self.lamp_small = TimedWizlight("lamp_small", "192.168.1.142")

        self.brightness_step = 75

        self.living_bulbs = {
            "kitchen": self.kitchen_light, 
            "living": self.living_light, 
            "lamp_big": self.lamp_big,
            "lamp_small": self.lamp_small
            }

        self.bulbs = {
            **self.living_bulbs,
            "bedroom": self.bedroom_light
        }

        self.rooms = {
            "living": self.living_bulbs,
            "bedroom": {"bedroom": self.bedroom_light}
        }

        self.captured_scenes = load_scenes()
        self.custom_bindings = load_bindings()

    def get_binding(self, action: ButtonAction):
        if action in self.custom_bindings:
            return self.custom_bindings[action]
        return DEFAULT_BINDINGS.get(action, Binding())

    def list_bindings(self):
        return {
            action.value: {
                "source": "custom" if action in self.custom_bindings else "default",
                "has_default": action in DEFAULT_BINDINGS,
                "label": self.get_binding(action).label,
                "binding": self.get_binding(action).model_dump(exclude_none=True)
            }
            for action in ButtonAction
        }

    def scene_button(self, room: str, slug: str):
        """The button action a scene is currently on, if any."""
        for action in ButtonAction:
            binding = self.get_binding(action)
            if binding.room == room and binding.scene == slug:
                return action.value
        return None

    def bind_scene(self, room: str, slug: str, action: ButtonAction | None):
        """Put a scene on a button, taking it off whatever button it was on."""
        self.get_scene(room, slug)

        for other in ButtonAction:
            binding = self.get_binding(other)
            if binding.room == room and binding.scene == slug and other != action:
                self._set_binding(other, Binding())

        if action is not None:
            self._set_binding(action, Binding(room=room, scene=slug))

        save_bindings(self.custom_bindings)
        return self.get_binding(action) if action else None

    def _set_binding(self, action: ButtonAction, binding: Binding):
        """Only bindings that differ from the default are worth storing."""
        if binding == DEFAULT_BINDINGS.get(action, Binding()):
            self.custom_bindings.pop(action, None)
        else:
            self.custom_bindings[action] = binding

    def reset_binding(self, action: ButtonAction):
        """Drop the custom binding so the hardcoded default applies again."""
        was_custom = self.custom_bindings.pop(action, None) is not None
        if was_custom:
            save_bindings(self.custom_bindings)
        return was_custom

    def room_bulbs(self, room: str):
        if room not in self.rooms:
            raise KeyError(f"Unknown room '{room}'")
        return self.rooms[room]

    def get_scene(self, room: str, slug: str):
        """Captured scene if there is one, the hardcoded default otherwise."""
        self.room_bulbs(room)
        scene = self.captured_scenes.get(room, {}).get(slug) or DEFAULT_SCENES.get(room, {}).get(slug)
        if scene is None:
            raise KeyError(f"Unknown scene '{room}/{slug}'")
        return scene

    def list_rooms(self):
        return {
            room: {
                "bulbs": {name: bulb.ip for name, bulb in bulbs.items()},
                "scenes": sorted({*DEFAULT_SCENES.get(room, {}), *self.captured_scenes.get(room, {})})
            }
            for room, bulbs in self.rooms.items()
        }

    def list_scenes(self, room: str):
        self.room_bulbs(room)
        slugs = {*DEFAULT_SCENES.get(room, {}), *self.captured_scenes.get(room, {})}
        return {slug: self.describe_scene(room, slug) for slug in sorted(slugs)}

    def describe_scene(self, room: str, slug: str):
        scene = self.get_scene(room, slug)
        return {
            "source": "captured" if slug in self.captured_scenes.get(room, {}) else "default",
            "has_default": slug in DEFAULT_SCENES.get(room, {}),
            "button": self.scene_button(room, slug),
            "bulbs": {name: state.model_dump(exclude_none=True) for name, state in scene.items()}
        }

    async def apply_scene(self, room: str, slug: str):
        scene = self.get_scene(room, slug)
        bulbs = self.room_bulbs(room)
        targets = [(name, state) for name, state in scene.items() if name in bulbs]

        return await gather_bulbs(
            [name for name, _ in targets],
            [self._apply_bulb_state(bulbs[name], state) for name, state in targets])

    async def capture_scene(self, room: str, slug: str):
        """Store what the bulbs of a room are doing right now as the scene."""
        bulbs = self.room_bulbs(room)
        names = list(bulbs)
        states = await asyncio.gather(
            *(bulbs[name].updateState() for name in names), return_exceptions=True)

        captured = {}
        silent = [name for name, state in zip(names, states)
                  if state is None or isinstance(state, Exception)]
        if silent:
            # Capturing half a room would quietly write a scene that is missing bulbs.
            raise RuntimeError(f"No response from {', '.join(silent)} - is it off at the wall?")

        for name, state in zip(names, states):
            captured[name] = BulbState.capture(state)

        self.captured_scenes.setdefault(room, {})[slug] = captured
        save_scenes(self.captured_scenes)
        return captured

    def delete_scene(self, room: str, slug: str):
        """Drop a captured scene for good, and take it off any button it was on."""
        self.room_bulbs(room)
        if slug in DEFAULT_SCENES.get(room, {}):
            raise ValueError(f"Scene '{room}/{slug}' is built in, reset it instead of deleting it")
        if slug not in self.captured_scenes.get(room, {}):
            raise KeyError(f"Unknown scene '{room}/{slug}'")

        for action in ButtonAction:
            binding = self.get_binding(action)
            if binding.room == room and binding.scene == slug:
                self._set_binding(action, Binding())

        save_bindings(self.custom_bindings)
        del self.captured_scenes[room][slug]
        save_scenes(self.captured_scenes)

    def reset_scene(self, room: str, slug: str):
        """Drop the captured scene so the hardcoded default applies again."""
        self.room_bulbs(room)
        if slug not in DEFAULT_SCENES.get(room, {}):
            raise KeyError(f"Scene '{room}/{slug}' has no default")

        was_captured = self.captured_scenes.get(room, {}).pop(slug, None) is not None
        if was_captured:
            save_scenes(self.captured_scenes)
        return was_captured

    async def _apply_bulb_state(self, bulb: wizlight, state: BulbState):
        if not state.state:
            await bulb.turn_off()
            return

        if state.scene is not None and state.scene not in SCENES:
            # Scene id the library does not know yet (newer app modes) - send it raw.
            params = {"state": True, "sceneId": state.scene}
            if state.brightness is not None:
                params["dimming"] = max(10, hex_to_percent(state.brightness))
            if state.speed is not None:
                params["speed"] = state.speed
            await bulb.send({"method": "setPilot", "params": params})
            return

        await bulb.turn_on(state.to_pilot())

    async def execute_command(self, c: WizCommand):
        living = list(self.living_bulbs)

        match c:
            case WizCommand.BEDROOM_TOGGLE:
                return await gather_bulbs(["bedroom"], [self._toggle_bulb(self.bedroom_light)])
            case WizCommand.BEDROOM_BRIGHTNESS_UP:
                return await gather_bulbs(["bedroom"], [self._modify_brightness(self.bedroom_light, self.brightness_step)])
            case WizCommand.BEDROOM_BRIGHTNESS_DOWN:
                return await gather_bulbs(["bedroom"], [self._modify_brightness(self.bedroom_light, -self.brightness_step)])
            case WizCommand.LIVING_TOGGLE:
                is_living_on = await self._is_living_on()
                return await gather_bulbs(living, [
                    self._toggle_bulb(self.living_bulbs[name], is_living_on) for name in living])
            case WizCommand.LIVING_BRIGHTNESS_UP:
                return await gather_bulbs(living, [
                    self._modify_brightness(self.living_bulbs[name], self.brightness_step) for name in living])
            case WizCommand.LIVING_BRIGHTNESS_DOWN:
                return await gather_bulbs(living, [
                    self._modify_brightness(self.living_bulbs[name], -self.brightness_step) for name in living])

    async def cleanup(self):
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            await bulb.async_close()

    async def _toggle_bulb(self, bulb: wizlight, is_on: bool | None = None):
        if is_on is None:
            state = await bulb.updateState()
            if state is None:
                return
            is_on = state.get_state()

        if is_on:
            await bulb.turn_off()
        else:
            await bulb.turn_on(PilotBuilder(warm_white=255, brightness=255))

    async def _is_living_on(self):
        """Whether any living bulb is on, without letting one sleepy bulb hold up the room.

        Every bulb is asked at once. The first bulb that says "on" settles it; otherwise whoever
        answered within STATE_CHECK_DEADLINE decides, and if nobody has by then, the first answer
        does. Bulbs still silent are given up on, so the command goes out to everyone right away
        and a sleepy bulb switches as soon as it wakes and hears the retransmitted setPilot."""
        start = time.monotonic()
        tasks = {asyncio.ensure_future(bulb.updateState()): name for name, bulb in self.living_bulbs.items()}
        pending = set(tasks)
        answers = {}
        deadline = start + STATE_CHECK_DEADLINE

        while pending and not any(answers.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0 and answers:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining if remaining > 0 else None, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is not None:
                    log(f"Bulb '{tasks[task]}' did not answer: {describe_bulb_error(task.exception())}")
                elif task.result() is not None:
                    answers[tasks[task]] = bool(task.result().get_state())

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # frees each bulb's lock for the setPilot

        is_on = any(answers.values())
        skipped = f", did not wait for {', '.join(tasks[t] for t in pending)}" if pending else ""
        log(f"living is {'on' if is_on else 'off'}, decided in {ms_since(start)}{skipped}")
        return is_on

    async def _modify_brightness(self, bulb: wizlight, step: int, min = 26, max = 255):
        state = await bulb.updateState()
        if state is None:
            return

        brightness = state.get_brightness()
        if brightness is None:
            return

        new_brightness = brightness + step
        if new_brightness < min:
            new_brightness = min
        elif new_brightness > max:
            new_brightness = max

        await bulb.turn_on(PilotBuilder(brightness=new_brightness))

    async def print_state(self):
        if not self.debug:
            return

        # The dump is not part of the press it brackets - keep its getPilots out of the timings.
        token = current_press.set(None)
        try:
            await self._print_state()
        finally:
            current_press.reset(token)

    async def _print_state(self):
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            try:
                state = await bulb.updateState()
            except WizLightError as e:
                log(f"{bulb_name} did not answer: {e!r}")
                continue

            if state is None:
                log(f"{bulb_name} did not answer")
                continue

            on_off = "on" if state.get_state() else "off"
            scene = state.get_scene() or state.get_scene_id()
            log(f"{bulb_name} is {on_off}. brightness {state.get_brightness()}; scene {scene}; warm {state.get_warm_white()}; cold {state.get_cold_white()}; rgb {state.get_rgb()}; colortemp {state.get_colortemp()}")

wiz = Wiz()
timings = TimingHistory(TIMINGS_FILE)
main_loop = None

async def execute_action(action: ButtonAction, receipt: Receipt):
    """Run a button action, timing every step from `receipt` - when the press reached us."""
    current_action.set(action.value)
    press = Press(action.value, receipt)
    current_press.set(press)
    binding = wiz.get_binding(action)
    upstream = f", {receipt.upstream_ms}ms from sender" if receipt.upstream_ms is not None else ""
    log(f"{receipt.source} press, executing {binding.label} "
        f"(started {press.record['queue_ms']}ms after receipt{upstream})")
    if binding.is_empty:
        press.finish(binding.label, [])
        return binding, []

    if wiz.debug:
        log("Bulbs state before command")
        await wiz.print_state()

    unreachable = []
    if binding.command is not None:
        unreachable = await wiz.execute_command(binding.command)
    else:
        try:
            unreachable = await wiz.apply_scene(binding.room, binding.scene)
        except KeyError as e:
            log(f"Button {action} points at a scene that is gone: {e.args[0]}")

    press.finish(binding.label, unreachable)

    if wiz.debug:
        log("Bulbs state after command")
        await wiz.print_state()
    return binding, unreachable

def with_unreachable(message: str, unreachable):
    return f"{message} (no answer from {', '.join(unreachable)})" if unreachable else message


class _Ping(asyncio.DatagramProtocol):
    def __init__(self):
        self.answered = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.answered.done():
            self.answered.set_result(None)

async def ping_bulb(bulb: wizlight, timeout: float = 2):
    """One getPilot on a socket of its own - pywizlight's per-bulb lock would otherwise make a press
    queue behind a keepalive that is waiting on a sleepy bulb. Seconds to answer, or None."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_Ping, remote_addr=(bulb.ip, bulb.port))
    start = time.monotonic()
    try:
        transport.sendto(b'{"method":"getPilot","params":{}}')
        await asyncio.wait_for(protocol.answered, timeout)
        return time.monotonic() - start
    except OSError:  # includes TimeoutError
        return None
    finally:
        transport.close()

async def keep_bulbs_awake():
    """After hours without traffic the bulbs took 1-4s to answer the first press. Poke each one
    every KEEPALIVE_INTERVAL so their Wi-Fi is not cold when a press comes; log the slow ones."""
    current_action.set("keepalive")
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        names = list(wiz.bulbs)
        results = await asyncio.gather(*(ping_bulb(wiz.bulbs[name]) for name in names), return_exceptions=True)

        slow = []
        for name, result in zip(names, results):
            if result is None or isinstance(result, BaseException):
                slow.append(f"{name} no answer")
            elif result * 1000 > KEEPALIVE_SLOW_MS:
                slow.append(f"{name} {result * 1000:.0f}ms")
        if slow:
            log(f"slow to answer: {', '.join(slow)}")

def on_connect(client, userdata, flags, reason_code, properties):
    log(f"MQTT connected with result code {reason_code}")
    client.subscribe("zigbee2mqtt/button")

def on_message(client, userdata, msg):
    log(f"MQTT message on {msg.topic}: {msg.payload.decode()}")
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        action = ButtonAction(payload["action"])
        receipt = Receipt.from_payload(payload)

        asyncio.run_coroutine_threadsafe(execute_action(action, receipt), main_loop)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log(f"Error processing message: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    log("App starting...")
    global main_loop
    main_loop = asyncio.get_running_loop()

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    username = os.getenv('MQTT_USERNAME')
    password = os.getenv('MQTT_PASSWORD')

    mqtt_client.username_pw_set(username, password)
    mqtt_client.connect("192.168.1.105")
    mqtt_client.loop_start()
    app.state.mqtt_client = mqtt_client

    keepalive = asyncio.create_task(keep_bulbs_awake())

    yield  # application runs here

    keepalive.cancel()

    # Shutdown logic
    app.state.mqtt_client.loop_stop()
    app.state.mqtt_client.disconnect()
    await wiz.cleanup()
    log("App shutdown complete")


app = FastAPI(lifespan=lifespan)

@app.post("/action")
async def execute_command(item: ActionRequest):
    binding, unreachable = await execute_action(item.action, Receipt(source="http"))
    return {
        "message": with_unreachable(f"Executed {binding.label}", unreachable),
        "binding": binding.model_dump(exclude_none=True),
        "unreachable": unreachable
    }

@app.post("/brightness_step")
def set_brightness_step(item: BrightnessStepRequest):
    wiz.brightness_step = item.brightness_step
    return {
        "message": "Brightness step set successfully",
        "command": item.brightness_step
    }

@app.get("/brightness_step")
def get_brightness_step():
    return {
        "message": "Brightness step",
        "command": wiz.brightness_step
    }

@app.get("/", response_class=HTMLResponse)
def ui():
    return UI_FILE.read_text(encoding="utf-8")

@app.get("/scene-names")
def scene_names():
    """Scene id -> name, so the ui can show 'Candlelight' instead of 29."""
    return SCENES

@app.get("/room")
def list_rooms():
    return wiz.list_rooms()

@app.get("/room/{room}/scenes")
def list_scenes(room: str):
    try:
        return wiz.list_scenes(room)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])

@app.get("/room/{room}/scenes/{scene}")
def get_scene(room: str, scene: str):
    try:
        return wiz.describe_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])

@app.get("/room/{room}/scenes/{scene}/trigger")
async def trigger_scene(room: str, scene: str):
    name = f"scene {room}/{scene}"
    current_action.set(name)
    press = Press(name, Receipt(source="http"))
    current_press.set(press)
    try:
        unreachable = await wiz.apply_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])
    press.finish(f"{room}/{scene}", unreachable)

    return {
        "message": with_unreachable(f"Scene {room}/{scene} triggered", unreachable),
        "scene": wiz.describe_scene(room, scene),
        "unreachable": unreachable
    }

@app.delete("/room/{room}/scenes/{scene}")
def delete_scene(room: str, scene: str):
    try:
        wiz.delete_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": f"Scene {room}/{scene} deleted"}

@app.get("/room/{room}/scenes/{scene}/capture")
async def capture_scene(room: str, scene: str):
    try:
        await wiz.capture_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])
    except RuntimeError as e:
        raise HTTPException(status_code=504, detail=str(e))

    return {
        "message": f"Scene {room}/{scene} captured",
        "scene": wiz.describe_scene(room, scene)
    }

@app.get("/buttons")
def list_bindings():
    return wiz.list_bindings()

@app.get("/buttons/{action}/default")
def reset_binding(action: ButtonAction):
    was_custom = wiz.reset_binding(action)
    return {
        "message": f"Button {action.value} reset to default" if was_custom else f"Button {action.value} was already the default",
        "buttons": wiz.list_bindings()
    }

@app.post("/room/{room}/scenes/{scene}/button")
def bind_scene(room: str, scene: str, item: ButtonRequest):
    """Put the scene on a button action, or on none of them."""
    try:
        wiz.bind_scene(room, scene, item.action)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])

    return {
        "message": f"Scene {room}/{scene} is on {item.action.value}" if item.action else f"Scene {room}/{scene} is not on a button",
        "scene": wiz.describe_scene(room, scene)
    }

@app.get("/room/{room}/scenes/{scene}/default")
def reset_scene(room: str, scene: str):
    try:
        was_captured = wiz.reset_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0])

    return {
        "message": f"Scene {room}/{scene} reset to default" if was_captured else f"Scene {room}/{scene} was already the default",
        "scene": wiz.describe_scene(room, scene)
    }

@app.get("/timings")
def recent_timings(limit: int = 50, slower_than: int = 0):
    """The last presses, newest first, with where the time went. `slower_than` is in ms."""
    return timings.recent(limit, slower_than)

@app.get("/debug")
def toggle_debug():
    wiz.debug = not wiz.debug
    return {
        "message": "Debug toggled",
        "command": wiz.debug
    }

async def main():

    try:   
        await wiz.execute_command(WizCommand.LIVING_TOGGLE)

        await wiz.print_state()
    finally:
        await wiz.cleanup()

if __name__ == "__main__":
    asyncio.run(main())