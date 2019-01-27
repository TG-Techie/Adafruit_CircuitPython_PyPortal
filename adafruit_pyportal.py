import gc
import time
import board
import busio
import pulseio
import audioio
import displayio
import neopixel
from digitalio import DigitalInOut, Direction

from adafruit_esp32spi import adafruit_esp32spi
from adafruit_bitmap_font import bitmap_font
from adafruit_display_text.text_area import TextArea
import adafruit_esp32spi.adafruit_esp32spi_requests as requests

try:
    from settings import settings
except ImportError:
    print("WiFi settings are kept in settings.py, please add them there!")
    raise


class PyPortal:
    def __init__(self, *, url, json_path=None, xml_path=None,
                 default_bg=None, backlight=None, status_neopixel=None,
                 text_font=None, text_position=None, text_color=0x808080,
                 time_between_requests=60, success_callback=None):
        self._backlight = None
        if backlight:
            self._backlight = pulseio.PWMOut(backlight)
        self.set_backlight(0)  # turn off backlight

        self._url = url
        self._json_path = json_path
        self._xml_path = xml_path
        self._time_between_requests = time_between_requests
        self._success_callback = success_callback

        if status_neopixel:
            self.neopix = neopixel.NeoPixel(status_neopixel, 1, brightness=0.2)
        else:
            self.neopix = None
        self.neo_status(0)

        # Make ESP32 connection
        esp32_cs = DigitalInOut(board.ESP_CS)
        esp32_ready = DigitalInOut(board.ESP_BUSY)
        esp32_gpio0 = DigitalInOut(board.ESP_GPIO0)
        esp32_reset = DigitalInOut(board.ESP_RESET)
        spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
        self._esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset, esp32_gpio0)

        for _ in range(3): # retries
            try:
                print("ESP firmware:", self._esp.firmware_version)
                break
            except RuntimeError:
                time.sleep(1)
                self._esp.reset()
        else:
            raise RuntimeError("Was not able to find ESP32")

        requests.set_interface(self._esp)

        self.splash = displayio.Group()
        board.DISPLAY.show(self.splash)
        if default_bg:
            self._bg_file = open(default_bg, "rb")
            background = displayio.OnDiskBitmap(self._bg_file)
            self._bg_sprite = displayio.Sprite(background, pixel_shader=displayio.ColorConverter(), position=(0,0))
            self.splash.append(self._bg_sprite)
            board.DISPLAY.wait_for_frame()

        if text_font:
            self._text_font = bitmap_font.load_font(text_font)
            self._text = None
            self._text_color = text_color
            self._text_position = text_position
            self.set_text("PyPortal")
        else:
            self._text_font = None
            self._text = None

        self.set_backlight(1.0)  # turn on backlight

    def set_text(self, val):
        if self._text_font:
            if self._text:
                self.splash.pop()
            self._text = TextArea(self._text_font, text=str(val))
            self._text.color = self._text_color
            self._text.x = self._text_position[0]
            self._text.y = self._text_position[1]
            self.splash.append(self._text.group)
            board.DISPLAY.wait_for_frame()

    def set_backlight(self, val):
        if not self._backlight:
            return
        val = max(0, min(1.0, val))
        self._backlight.duty_cycle = int(val * 65535)

    def neo_status(self, value):
        if self.neopix:
            self.neopix.fill(value)

    def play_file(self, file_name):
        #self._speaker_enable.value = True
        with audioio.AudioOut(board.AUDIO_OUT) as audio:
            with open(file_name, "rb") as f:
                with audioio.WaveFile(f) as wavefile:
                    audio.play(wavefile)
                    while audio.playing:
                        pass
        #self._speaker_enable.value = False

    def fetch(self):
        gc.collect()

        self.neo_status((0, 0, 100))
        while not self._esp.is_connected:
            # settings dictionary must contain 'ssid' and 'password' at a minimum
            self.neo_status((100, 0, 0)) # red = not connected
            self._esp.connect(settings)
        # great, lets get the data
        print("Retrieving data...", end='')
        self.neo_status((100, 100, 0))   # yellow = fetching data
        r = requests.get(self._url)
        self.neo_status((0, 0, 100))   # green = got data
        print("Reply is OK!")

        value = None
        if self._json_path:
            value = r.json()
            for x in self._json_path:
                value = value[x]
        else:
            value = r.text()
        if self._success_callback:
            self._success_callback(value)
        gc.collect()
        self.set_text(value)
        return value
