import asyncio

from enum import Enum
from pathlib import Path
from pywizlight import wizlight, PilotBuilder
from pywizlight.bulb import PilotParser
from pywizlight.scenes import SCENES
from pywizlight.utils import hex_to_percent
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import paho.mqtt.client as mqtt
import os
import json

SCENES_FILE = Path(__file__).with_name("scenes.json")

class WizCommand(str, Enum):
    BEDROOM_TOGGLE = 'BEDROOM_TOGGLE'
    BEDROOM_BRIGHTNESS_UP = 'BEDROOM_BRIGHTNESS_UP'
    BEDROOM_BRIGHTNESS_DOWN = 'BEDROOM_BRIGHTNESS_DOWN'
    BEDROOM_DIM_LIGHT = 'BEDROOM_DIM_LIGHT'
    BEDROOM_BEDTIME = 'BEDROOM_BEDTIME'

    LIVING_TOGGLE = 'LIVING_TOGGLE'
    LIVING_BRIGHTNESS_UP = 'LIVING_BRIGHTNESS_UP'
    LIVING_BRIGHTNESS_DOWN = 'LIVING_BRIGHTNESS_DOWN'
    LIVING_NIGHT_TV = 'LIVING_NIGHT_TV'
    LIVING_COOKING = 'LIVING_COOKING'
    LIVING_GUESTS = 'LIVING_GUESTS'

SCENE_COMMANDS = {
    WizCommand.BEDROOM_DIM_LIGHT: ("bedroom", "dim-light"),
    WizCommand.BEDROOM_BEDTIME: ("bedroom", "bedtime"),

    WizCommand.LIVING_NIGHT_TV: ("living", "night-tv"),
    WizCommand.LIVING_COOKING: ("living", "cooking"),
    WizCommand.LIVING_GUESTS: ("living", "guests"),
}

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

class BulbState(BaseModel):
    """What a single bulb should look like as part of a scene."""
    state: bool = True
    scene: int | None = None
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
            return cls(scene=scene, brightness=brightness)

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

class Wiz:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Wiz, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.debug = False
        self.kitchen_light = wizlight("192.168.1.129")
        self.living_light = wizlight("192.168.1.130")
        self.bedroom_light = wizlight("192.168.1.135")  
        self.lamp_big = wizlight("192.168.1.254")
        self.lamp_small = wizlight("192.168.1.142")

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
            "bulbs": {name: state.model_dump(exclude_none=True) for name, state in scene.items()}
        }

    async def apply_scene(self, room: str, slug: str):
        scene = self.get_scene(room, slug)
        bulbs = self.room_bulbs(room)
        await asyncio.gather(*(
            self._apply_bulb_state(bulbs[name], state)
            for name, state in scene.items() if name in bulbs))
        return scene

    async def capture_scene(self, room: str, slug: str):
        """Store what the bulbs of a room are doing right now as the scene."""
        bulbs = self.room_bulbs(room)
        names = list(bulbs)
        states = await asyncio.gather(*(bulbs[name].updateState() for name in names))

        captured = {}
        for name, state in zip(names, states):
            if state is None:
                raise RuntimeError(f"No response from bulb '{name}'")
            captured[name] = BulbState.capture(state)

        self.captured_scenes.setdefault(room, {})[slug] = captured
        save_scenes(self.captured_scenes)
        return captured

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
            await bulb.send({"method": "setPilot", "params": params})
            return

        await bulb.turn_on(state.to_pilot())

    async def execute_command(self, c: WizCommand):
        if c in SCENE_COMMANDS:
            await self.apply_scene(*SCENE_COMMANDS[c])
            return

        match c:
            case WizCommand.BEDROOM_TOGGLE:
                await self._toggle_bulb(self.bedroom_light)
            case WizCommand.BEDROOM_BRIGHTNESS_UP:
                await self._modify_brightness(self.bedroom_light, self.brightness_step)
            case WizCommand.BEDROOM_BRIGHTNESS_DOWN:
                await self._modify_brightness(self.bedroom_light, -self.brightness_step)
            case WizCommand.LIVING_TOGGLE:
                is_living_on = await self._is_living_on()
                await asyncio.gather(
                    self._toggle_bulb(self.kitchen_light, is_living_on),
                    self._toggle_bulb(self.living_light, is_living_on),
                    self._toggle_bulb(self.lamp_big, is_living_on),
                    self._toggle_bulb(self.lamp_small, is_living_on))
            case WizCommand.LIVING_BRIGHTNESS_UP:
                await asyncio.gather(
                    self._modify_brightness(self.kitchen_light, self.brightness_step),
                    self._modify_brightness(self.living_light, self.brightness_step), 
                    self._modify_brightness(self.lamp_big, self.brightness_step),
                    self._modify_brightness(self.lamp_small, self.brightness_step))
            case WizCommand.LIVING_BRIGHTNESS_DOWN:
                await asyncio.gather(
                    self._modify_brightness(self.kitchen_light, -self.brightness_step),
                    self._modify_brightness(self.living_light, -self.brightness_step),
                    self._modify_brightness(self.lamp_big, -self.brightness_step),
                    self._modify_brightness(self.lamp_small, -self.brightness_step))

    async def cleanup(self):
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            await bulb.async_close()

    async def _toggle_bulb(self, bulb: wizlight, is_on: bool | None = None):
        state = (await bulb.updateState()).get_state() if is_on is None else is_on
        if state:
            await bulb.turn_off()
        else:
            await bulb.turn_on(PilotBuilder(warm_white=255, brightness=255))
    
    async def _is_living_on(self):
        for bulb_name in self.living_bulbs:
            bulb = self.living_bulbs[bulb_name]
            state = await bulb.updateState()
            if state.get_state():
                return True
            
        return False
               
    async def _modify_brightness(self, bulb: wizlight, step: int, min = 26, max = 255):
        state = await bulb.updateState()
        brightness = state.get_brightness()
        new_brightness = brightness + step
        if new_brightness < min:
            new_brightness = min
        elif new_brightness > max:
            new_brightness = max

        await bulb.turn_on(PilotBuilder(brightness=new_brightness))

    async def print_state(self):
        if not self.debug:
            return
        
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            state = await bulb.updateState()
            on_off = "on" if state.get_state() else "off"
            scene = state.get_scene() or state.get_scene_id()
            print(f"{bulb_name} is {on_off}. brightness {state.get_brightness()}; scene {scene}; warm {state.get_warm_white()}; cold {state.get_cold_white()}; rgb {state.get_rgb()}; colortemp {state.get_colortemp()}")

def map_action_to_command(action: ButtonAction):
    match action:
        case ButtonAction.single_button_1:
            return WizCommand.LIVING_TOGGLE
        case ButtonAction.single_button_3:
            return WizCommand.LIVING_NIGHT_TV
        case ButtonAction.double_button_1:
            return WizCommand.LIVING_COOKING
        case ButtonAction.double_button_3:
            return WizCommand.LIVING_GUESTS
        case ButtonAction.long_button_1:
            return WizCommand.LIVING_BRIGHTNESS_UP
        case ButtonAction.long_button_3:
            return WizCommand.LIVING_BRIGHTNESS_DOWN
        
        case ButtonAction.single_button_2:
            return WizCommand.BEDROOM_TOGGLE
        case ButtonAction.single_button_4:
            return WizCommand.BEDROOM_BEDTIME
        case ButtonAction.double_button_4:
            return WizCommand.BEDROOM_DIM_LIGHT
        case ButtonAction.long_button_2:
            return WizCommand.BEDROOM_BRIGHTNESS_UP
        case ButtonAction.long_button_4:
            return WizCommand.BEDROOM_BRIGHTNESS_DOWN
        
wiz = Wiz()
main_loop = None

async def execute_action(action: ButtonAction):
    command = map_action_to_command(action)
    print(f"Received button action {action}. Executing command {command}")
    if wiz.debug:
        print("Bulbs state before command")
        await wiz.print_state()

    await wiz.execute_command(command)
    
    if wiz.debug:
        print("Bulbs state after command")
        await wiz.print_state()
    return command


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected with result code {reason_code}")
    client.subscribe("zigbee2mqtt/button")

def on_message(client, userdata, msg):
    print(f"Received message on topic {msg.topic}: {msg.payload.decode()}")
    try:
            payload = json.loads(msg.payload.decode('utf-8'))    
            action = payload["action"]

            asyncio.run_coroutine_threadsafe(execute_action(ButtonAction(action)), main_loop)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"Error processing message: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App starting...")
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

    yield  # application runs here

    # Shutdown logic
    app.state.mqtt_client.loop_stop()
    app.state.mqtt_client.disconnect()
    await wiz.cleanup()
    print("App shutdown complete")


app = FastAPI(lifespan=lifespan)

@app.post("/action")
async def execute_command(item: ActionRequest):
    command = await execute_action(item.action)
    return {
        "message": "Command executed",
        "command": command
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

@app.get("/room")
def list_rooms():
    return wiz.list_rooms()

@app.get("/room/{room}/scenes")
def list_scenes(room: str):
    try:
        return wiz.list_scenes(room)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/room/{room}/scenes/{scene}")
def get_scene(room: str, scene: str):
    try:
        return wiz.describe_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/room/{room}/scenes/{scene}/apply")
async def apply_scene(room: str, scene: str):
    try:
        await wiz.apply_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "message": f"Scene {room}/{scene} applied",
        "scene": wiz.describe_scene(room, scene)
    }

@app.get("/room/{room}/scenes/{scene}/capture")
async def capture_scene(room: str, scene: str):
    try:
        await wiz.capture_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=504, detail=str(e))

    return {
        "message": f"Scene {room}/{scene} captured",
        "scene": wiz.describe_scene(room, scene)
    }

@app.get("/room/{room}/scenes/{scene}/default")
def reset_scene(room: str, scene: str):
    try:
        was_captured = wiz.reset_scene(room, scene)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "message": f"Scene {room}/{scene} reset to default" if was_captured else f"Scene {room}/{scene} was already the default",
        "scene": wiz.describe_scene(room, scene)
    }

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