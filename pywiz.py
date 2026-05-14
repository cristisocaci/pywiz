import asyncio

from enum import Enum
from pywizlight import wizlight, PilotBuilder
from fastapi import FastAPI
from pydantic import BaseModel
from contextlib import asynccontextmanager

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

class Wiz:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Wiz, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.kitchen_light = wizlight("192.168.1.129")
        self.living_light = wizlight("192.168.1.130")
        self.bedroom_light = wizlight("192.168.1.135")  

        self.brightness_step = 20

        self.bulbs = {"kitchen": self.kitchen_light, "living": self.living_light, "bedroom": self.bedroom_light}

    async def execute_command(self, c: WizCommand):
        match c:
            case WizCommand.BEDROOM_TOGGLE:
                await self._toggle_bulb(self.bedroom_light)
            case WizCommand.BEDROOM_BRIGHTNESS_UP:
                self._modify_brightness(self.bedroom_light, self.brightness_step)
            case WizCommand.BEDROOM_BRIGHTNESS_DOWN:
                self._modify_brightness(self.bedroom_light, -self.brightness_step)
            case WizCommand.BEDROOM_DIM_LIGHT:
                await self.bedroom_light.turn_on(PilotBuilder(warm_white=255, brightness=125))
            case WizCommand.BEDROOM_BEDTIME:
                await self.bedroom_light.turn_on(PilotBuilder(scene=6, brightness=90)) # cozy

            case WizCommand.LIVING_TOGGLE:
                await self._toggle_bulb(self.kitchen_light)
                await self._toggle_bulb(self.living_light)
            case WizCommand.LIVING_BRIGHTNESS_UP:
                await self._modify_brightness(self.kitchen_light, self.brightness_step)
                await self._modify_brightness(self.living_light, self.brightness_step)
            case WizCommand.LIVING_BRIGHTNESS_DOWN:
                await self._modify_brightness(self.kitchen_light, -self.brightness_step)
                await self._modify_brightness(self.living_light, -self.brightness_step)
            case WizCommand.LIVING_NIGHT_TV:
                await self.kitchen_light.turn_on(PilotBuilder(warm_white=255, brightness=100))
                await self.living_light.turn_on(PilotBuilder(warm_white=255, brightness=26))
            case WizCommand.LIVING_COOKING:
                await self.kitchen_light.turn_on(PilotBuilder(cold_white=255, brightness=255))
                await self.living_light.turn_on(PilotBuilder(warm_white=255, brightness=190))
            case WizCommand.LIVING_GUESTS:
                await self.kitchen_light.turn_on(PilotBuilder(warm_white=255, brightness=125))
                await self.living_light.turn_on(PilotBuilder(warm_white=255, brightness=255))
    
    async def cleanup(self):
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            await bulb.async_close()

    async def _toggle_bulb(self, bulb: wizlight):
        state = await bulb.updateState()
        if state.get_state():
            await bulb.turn_off()
        else:
            await bulb.turn_on(PilotBuilder(brightness=255))
    
    async def _modify_brightness(self, bulb: wizlight, step: int, min = 26, max = 255):
        state = await bulb.updateState()
        brightness = state.get_brightness()
        print(brightness)
        new_brightness = brightness + step
        if new_brightness < min:
            new_brightness = min
        elif new_brightness > max:
            new_brightness = max

        await bulb.turn_on(PilotBuilder(brightness=new_brightness))

    async def _print_state(self):
        for bulb_name in self.bulbs:
            bulb = self.bulbs[bulb_name]
            state = await bulb.updateState()
            on_off = "on" if state.get_state() else "off"
            print(f"{bulb_name} is {on_off}. brightness {state.get_brightness()}; warm {state.get_warm_white()}; cold {state.get_cold_white()}; rgb {state.get_rgb()}; colortemp {state.get_colortemp()}")


def map_action_to_command(action: ButtonAction):
    match action:
        case ButtonAction.single_button_1:
            return WizCommand.LIVING_TOGGLE
        
        case ButtonAction.single_button_2:
            return WizCommand.BEDROOM_TOGGLE

wiz = Wiz()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (if needed)
    print("App starting...")

    yield  # application runs here

    # Shutdown logic
    await wiz.cleanup()
    print("App shutdown complete")


app = FastAPI(lifespan=lifespan)

@app.post("/action")
async def execute_command(item: ActionRequest):
    command = map_action_to_command(item.action)
    await wiz.execute_command(command)
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

async def main():

    try:   
        await wiz.execute_command(WizCommand.BEDROOM_BEDTIME)

        await wiz._print_state()
    finally:
        await wiz.cleanup()

if __name__ == "__main__":
    asyncio.run(main())