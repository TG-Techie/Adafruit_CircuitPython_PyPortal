import os
import gc
import supervisor
import time
import board
import busio
import pulseio
import audioio
import displayio
import neopixel
import microcontroller
from digitalio import DigitalInOut, Direction
import adafruit_touchscreen

from adafruit_esp32spi import adafruit_esp32spi
from adafruit_bitmap_font import bitmap_font
from adafruit_display_text.text_area import TextArea
import adafruit_esp32spi.adafruit_esp32spi_requests as requests

try:
    from settings import settings
except ImportError:
    print("""WiFi settings are kept in settings.py, please add them there!
the settings dictionary must contain 'ssid' and 'password' at a minimum""")
    raise

IMAGE_CONVERTER_SERVICE = "https://res.cloudinary.com/schmarty/image/fetch/w_320,h_240,c_fill,f_bmp/"
#IMAGE_CONVERTER_SERVICE = "http://ec2-107-23-37-170.compute-1.amazonaws.com/rx/ofmt_bmp,rz_320x240/"
LOCALFILE = "local.txt"

class fake_requests:
    def __init__(self, filename):
        self._filename=filename
        with open(filename, "r")  as f:
            self.text = f.read()
    def json(self):
        import ujson
        return ujson.loads(self.text)

class PyPortal:
    def __init__(self, *, url, json_path=None, xml_path=None,
                 default_bg=None, status_neopixel=None,
                 text_font=None, text_position=None, text_color=0x808080,
                 text_wrap=0, text_maxlen=0,
                 image_json_path=None, image_resize=None, image_position=None,
                 time_between_requests=60, success_callback=None,
                 caption_text=None, caption_font=None, caption_position=None,
                 caption_color=0x808080,
                 debug=True):

        self._debug = debug

        try:
            self._backlight = pulseio.PWMOut(board.TFT_BACKLIGHT)
        except:
            pass
        self.set_backlight(1.0)  # turn on backlight

        self._url = url
        if json_path:
            if isinstance(json_path[0], tuple) or isinstance(json_path[0], list):
                self._json_path = json_path
            else:
                self._json_path = (json_path,)
        else:
            self._json_path = None

        self._xml_path = xml_path
        self._time_between_requests = time_between_requests
        self._success_callback = success_callback

        if status_neopixel:
            self.neopix = neopixel.NeoPixel(status_neopixel, 1, brightness=0.2)
        else:
            self.neopix = None
        self.neo_status(0)

        # Make ESP32 connection
        if self._debug:
            print("Init ESP32")
        esp32_cs = DigitalInOut(microcontroller.pin.PB14)
        esp32_ready = DigitalInOut(microcontroller.pin.PB16)
        esp32_gpio0 = DigitalInOut(microcontroller.pin.PB15)
        esp32_reset = DigitalInOut(microcontroller.pin.PB17)
        spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
        self._esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset, esp32_gpio0)
        #self._esp._debug = 1
        for _ in range(3): # retries
            try:
                print("ESP firmware:", self._esp.firmware_version)
                break
            except RuntimeError:
                print("Retrying ESP32 connection")
                time.sleep(1)
                self._esp.reset()
        else:
            raise RuntimeError("Was not able to find ESP32")

        requests.set_interface(self._esp)

        if self._debug:
            print("Init display")
        self.splash = displayio.Group(max_size=5)
        board.DISPLAY.show(self.splash)

        if self._debug:
            print("Init background")
        self._bg_group = displayio.Group(max_size=1)
        self._bg_file = None
        self.set_background(default_bg)
        self.splash.append(self._bg_group)

        self._qr_group = None

        if self._debug:
            print("Init caption")
        self._caption=None
        if caption_font:
            self._caption_font = bitmap_font.load_font(caption_font)
        self.set_caption(caption_text, caption_position, caption_color)


        if text_font:
            if isinstance(text_position[0], tuple) or isinstance(text_position[0], list):
                num = len(text_position)
                if not text_wrap:
                    text_wrap = [0] * num
                if not text_maxlen:
                    text_maxlen = [0] * num
            else:
                num = 1
                text_position = (text_position,)
                text_color = (text_color,)
                text_wrap = (text_wrap,)
                text_maxlen = (text_maxlen,)
            self._text = [None] * num
            self._text_color = [None] * num
            self._text_position = [None] * num
            self._text_wrap = [None] * num
            self._text_maxlen = [None] * num
            self._text_font = bitmap_font.load_font(text_font)
            if self._debug:
                print("Loading font glyphs")
            #self._text_font.load_glyphs(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:/-_,. ')
            gc.collect()

            for i in range(num):
                if self._debug:
                    print("Init text area", i)
                self._text[i] = None
                self._text_color[i] = text_color[i]
                self._text_position[i] = text_position[i]
                self._text_wrap[i] = text_wrap[i]
                self._text_maxlen[i] = text_maxlen[i]
        else:
            self._text_font = None
            self._text = None

        self._image_json_path = image_json_path
        self._image_resize = image_resize
        self._image_position = image_position
        if image_json_path:
            if self._debug:
                print("Init image path")
            if not self._image_position:
                self._image_position = (0, 0)  # default to top corner
            if not self._image_resize:
                self._image_resize = (320, 240)  # default to full screen

        if self._debug:
            print("Init touchscreen")
        self.ts = adafruit_touchscreen.Touchscreen(microcontroller.pin.PB01, microcontroller.pin.PB08,
                                                   microcontroller.pin.PA06, microcontroller.pin.PB00,
                                                   calibration=((5200, 59000), (5800, 57000)),
                                                   size=(320, 240))

        self.set_backlight(1.0)  # turn on backlight
        gc.collect()

    def set_background(self, filename):
        print("Set background to ", filename)
        try:
            self._bg_group.pop()
        except IndexError:
            pass # s'ok, we'll fix to test once we can

        if not filename:
            return # we're done, no background desired
        if self._bg_file:
            self._bg_file.close()
        self._bg_file = open(filename, "rb")
        background = displayio.OnDiskBitmap(self._bg_file)
        try:
            self._bg_sprite = displayio.TileGrid(background, pixel_shader=displayio.ColorConverter(), position=(0,0))
        except:
            self._bg_sprite = displayio.Sprite(background, pixel_shader=displayio.ColorConverter(), position=(0,0))

        self._bg_group.append(self._bg_sprite)
        board.DISPLAY.refresh_soon()
        gc.collect()
        board.DISPLAY.wait_for_frame()

    def set_backlight(self, val):
        if not self._backlight:
            return
        val = max(0, min(1.0, val))
        self._backlight.duty_cycle = int(val * 65535)

    def set_caption(self, caption_text, caption_position, caption_color):
        if self._debug:
            print("Setting caption to", caption_text)

        if (not caption_text) or (not self._caption_font) or (not caption_position):
            return  # nothing to do!

        if self._caption:
            self._caption._update_text(str(val))
            board.DISPLAY.refresh_soon()
            board.DISPLAY.wait_for_frame()
            return

        self._caption = TextArea(self._caption_font, text=str(caption_text))
        self._caption.x = caption_position[0]
        self._caption.y = caption_position[1]
        self._caption.color = caption_color
        self.splash.append(self._caption.group)

    def set_text(self, val, index=0):
        if self._text_font:
            string = str(val)
            if self._text_maxlen[index]:
                string = string[:self._text_maxlen[index]]
            if self._text[index]:
                # TODO: repalce this with a simple set_text() once that works well
                items = []
                while True:
                    try:
                        item = self.splash.pop()
                        if item == self._text[index].group:
                            break
                        items.append(item)
                    except IndexError:
                        break
                self._text[index] = TextArea(self._text_font, text=string)
                self._text[index].color = self._text_color[index]
                self._text[index].x = self._text_position[index][0]
                self._text[index].y = self._text_position[index][1]
                self.splash.append(self._text[index].group)
                for g in items:
                    self.splash.append(g)
                return
            if self._text_position[index]:  # if we want it placed somewhere...
                self._text[index] = TextArea(self._text_font, text=string)
                self._text[index].color = self._text_color[index]
                self._text[index].x = self._text_position[index][0]
                self._text[index].y = self._text_position[index][1]
                self.splash.append(self._text[index].group)

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

    def _json_pather(self, json, path):
        value = json
        for x in path:
            value = value[x]
            gc.collect()
        return value

    def wget(self, url, filename):
        print("Fetching stream from", url)

        self.neo_status((100, 100, 0))
        r = requests.get(url, stream=True)

        if self._debug:
            print(r.headers)
        content_length = int(r.headers['content-length'])
        remaining = content_length
        print("Saving data to ", filename)
        stamp = time.monotonic()
        with open(filename, "wb") as f:
            for i in r.iter_content(min(remaining, 12000)):  # huge chunks!
                self.neo_status((0, 100, 100))
                remaining -= len(i)
                f.write(i)
                if self._debug:
                    print("Read %d bytes, %d remaining" % (content_length-remaining, remaining))
                else:
                    print(".", end='')
                if not remaining:
                    break
                self.neo_status((100, 100, 0))

        r.close()
        stamp = time.monotonic() - stamp
        print("Created file of %d bytes in %0.1f seconds" % (os.stat(filename)[6], stamp))
        self.neo_status((0, 0, 0))

    def fetch(self):
        json_out = None
        image_url = None
        values = []

        gc.collect()
        if self._debug:
            print("Free mem: ", gc.mem_free())

        r = None
        try:
            os.stat(LOCALFILE)
            print("*** USING LOCALFILE FOR DATA - NOT INTERNET!!! ***")
            r = fake_requests(LOCALFILE)
        except OSError:
            pass

        if not r:
            self.neo_status((0, 0, 100))
            while not self._esp.is_connected:
                if self._debug:
                    print("Connecting to AP")
                # settings dictionary must contain 'ssid' and 'password' at a minimum
                self.neo_status((100, 0, 0)) # red = not connected
                self._esp.connect(settings)
            # great, lets get the data
            print("Retrieving data...", end='')
            self.neo_status((100, 100, 0))   # yellow = fetching data
            gc.collect()
            r = requests.get(self._url)
            gc.collect()
            self.neo_status((0, 0, 100))   # green = got data
            print("Reply is OK!")

        if self._debug:
            print(r.text)


        if self._image_json_path or self._json_path:
            try:
                gc.collect()
                json_out = r.json()
                gc.collect()
            except ValueError:            # failed to parse?
                print("Couldn't parse json: ", r.text)
                raise
            except MemoryError:
                supervisor.reload()

        if self._xml_path:
            try:
                import xmltok
                print("*"*40)
                tokens = []
                for i in xmltok.tokenize(r.text):
                    print(i)
                print(tokens)
                print("*"*40)
            except ValueError:            # failed to parse?
                print("Couldn't parse XML: ", r.text)
                raise


        # extract desired text/values from json
        if self._json_path:
            for path in self._json_path:
                values.append(self._json_pather(json_out, path))
        else:
            values = r.text

        if self._image_json_path:
            image_url = self._json_pather(json_out, self._image_json_path)

        # we're done with the requests object, lets delete it so we can do more!
        json_out = None
        r = None
        gc.collect()

        if image_url:
            print("original URL:", image_url)
            image_url = IMAGE_CONVERTER_SERVICE+image_url
            print("convert URL:", image_url)
            # convert image to bitmap and cache
            #print("**not actually wgetting**")
            self.wget(image_url, "/cache.bmp")
            self.set_background("/cache.bmp")
            image_url = None
            gc.collect()

        # if we have a callback registered, call it now
        if self._success_callback:
            self._success_callback(values)

        # fill out all the text blocks
        if self._text:
            for i in range(len(self._text)):
                string = None
                try:
                    string = "{:,d}".format(int(values[i]))
                except ValueError:
                    string = values[i] # ok its a string
                if self._debug:
                    print("Drawing text", string)
                if self._text_wrap[i]:
                    if self._debug:
                        print("Wrapping text")
                    string = '\n'.join(self.wrap_nicely(string, self._text_wrap[i]))
                self.set_text(string, index=i)
        if len(values) == 1:
            return values[0]
        return values

    def show_QR(self, qr_data, qr_size=128, position=None):
        import adafruit_miniqr

        if not qr_data: # delete it
            if self._qr_group:
                try:
                    self._qr_group.pop()
                except IndexError:
                    pass
                board.DISPLAY.refresh_soon()
                board.DISPLAY.wait_for_frame()
            return

        if not position:
            position=(0, 0)
        if qr_size % 32 != 0:
            raise RuntimeError("QR size must be divisible by 32")

        qr = adafruit_miniqr.QRCode()
        qr.add_data(qr_data)
        qr.make()

        # how big each pixel is, add 2 blocks on either side
        BLOCK_SIZE = qr_size // (qr.matrix.width+4)
        # Center the QR code in the middle
        X_OFFSET = (qr_size - BLOCK_SIZE * qr.matrix.width) // 2
        Y_OFFSET = (qr_size - BLOCK_SIZE * qr.matrix.height) // 2

        # monochome (2 color) palette
        palette = displayio.Palette(2)
        palette[0] = 0xFFFFFF
        palette[1] = 0x000000

        # bitmap the size of the matrix + borders, monochrome (2 colors)
        qr_bitmap = displayio.Bitmap(qr_size, qr_size, 2)

        # raster the QR code
        line = bytearray(qr_size // 8)  # monochrome means 8 pixels per byte
        for y in range(qr.matrix.height):    # each scanline in the height
            for i, _ in enumerate(line):    # initialize it to be empty
                line[i] = 0
            for x in range(qr.matrix.width):
                if qr.matrix[x, y]:
                    for b in range(BLOCK_SIZE):
                        _x = X_OFFSET + x * BLOCK_SIZE + b
                        line[_x // 8] |= 1 << (7-(_x % 8))

            for b in range(BLOCK_SIZE):
                # load this line of data in, as many time as block size
                qr_bitmap._load_row(Y_OFFSET + y*BLOCK_SIZE+b, line) #pylint: disable=protected-access

        # display the bitmap using our palette
        qr_sprite = displayio.Sprite(qr_bitmap, pixel_shader=palette, position=position)
        if self._qr_group:
            try:
                self._qr_group.pop()
            except IndexError: # later test if empty
                pass
        else:
            self._qr_group = displayio.Group()
            self.splash.append(self._qr_group)
        self._qr_group.append(qr_sprite)
        board.DISPLAY.refresh_soon()
        board.DISPLAY.wait_for_frame()

    # return a list of lines with wordwrapping
    def wrap_nicely(self, string, max_chars):
        words = string.split(' ')
        the_lines = []
        the_line = ""
        for w in words:
            if len(the_line+' '+w) <= max_chars:
                the_line += ' '+w
            else:
                the_lines.append(the_line)
                the_line = ''+w
        if the_line:      # last line remaining
            the_lines.append(the_line)
        return the_lines
