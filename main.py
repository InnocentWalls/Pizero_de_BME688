import time
import colorsys
import os
import sys
import socket
import ST7735
import datetime

try:
    # Transitional fix for breaking change in LTR559
    from ltr559 import LTR559
    ltr559 = LTR559()
except ImportError:
    import ltr559


#fvalent
from pms5003 import PMS5003

from bme280 import BME280
from enviroplus import gas
from subprocess import PIPE, Popen
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from influxdb import InfluxDBClient

try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus

print("""

Press Ctrl+C to exit!

""")

# BME280 temperature/pressure/humidity sensor
bus = SMBus(1)
bme280 = BME280(i2c_dev=bus)

#fvalent
pms5003 = PMS5003()

# Create ST7735 LCD display class
st7735 = ST7735.ST7735(
    port=0,
    cs=1,
    dc=9,
    backlight=12,
    rotation=270,
    spi_speed_hz=10000000
)

# Initialize display
st7735.begin()

WIDTH = st7735.width
HEIGHT = st7735.height

# Set up canvas and font
# img = Image.new('RGB', (WIDTH, HEIGHT), color=(0, 0, 0))
# draw = ImageDraw.Draw(img)
# path = os.path.dirname(os.path.realpath(__file__))
# font = ImageFont.truetype(path + "/fonts/Asap/Asap-Bold.ttf", 20)

# Set up InfluxDB
influx = InfluxDBClient(host="192.168.100.7",
                        port="8086",
                        username="grafana",
                        password="grafana",
                        database="urban")


influx_json_prototyp = [
        {
            "measurement": "enviroplus",
            "tags": {
                "host": "enviroplus"
            },
            "fields": {
            }
        }
    ]

# The position of the top bar
top_pos = 25





# Get the temperature of the CPU for compensation
def get_cpu_temperature():
    process = Popen(['vcgencmd', 'measure_temp'], stdout=PIPE)
    output, _error = process.communicate()
    output = output.decode()
    return float(output[output.index('=') + 1:output.rindex("'")])

# Get local IP address
def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


# Tuning factor for compensation. Decrease this number to adjust the
# temperature down, and increase to adjust up
factor = 0.8

cpu_temps = [0] * 5

delay = 0.5  # Debounce the proximity tap
mode = 0  # The starting mode
last_page = 0
light = 1






# Create a values dict to store the data
variables = ["temperature",
             "pressure",
             "humidity",
             "light",
             "oxidised",
             "reduced",
             "nh3",
             "pm010",
             "pm025",
             "pm100"]

values = {}

for v in variables:
    values[v] = [1] * WIDTH

# The main loop
try:
    iterations = 0
    while True:
        current_time = datetime.datetime.utcnow().isoformat() + "Z"
        proximity = ltr559.get_proximity()

        # Compensated Temperature
        cpu_temp = get_cpu_temperature()
        # Smooth out with some averaging to decrease jitter
        cpu_temps = cpu_temps[1:] + [cpu_temp]
        avg_cpu_temp = sum(cpu_temps) / float(len(cpu_temps))
        raw_temp = bme280.get_temperature()
        compensated_temp = raw_temp - ((avg_cpu_temp - raw_temp) / factor)

        # Change json
        influx_json_prototyp[0]['fields']['ltr559.proximity'] = proximity

        if proximity < 10:
            influx_json_prototyp[0]['fields']['ltr559.lux'] = ltr559.get_lux()
        else:
            influx_json_prototyp[0]['fields']['ltr559.lux'] = 1.0

        if iterations >= 6:
            influx_json_prototyp[0]['fields']['bme280.temperature.raw'] = bme280.get_temperature()
            influx_json_prototyp[0]['fields']['bme280.temperature.compensated'] = compensated_temp

        influx_json_prototyp[0]['fields']['cpu.temperature'] = get_cpu_temperature()
        influx_json_prototyp[0]['fields']['bme280.pressure'] = bme280.get_pressure()
        influx_json_prototyp[0]['fields']['bme280.humidity'] = bme280.get_humidity()

        gas_data = gas.read_all()
        influx_json_prototyp[0]['fields']['mics6814.oxidising'] = gas_data.oxidising
        influx_json_prototyp[0]['fields']['mics6814.reducing'] = gas_data.reducing
        influx_json_prototyp[0]['fields']['mics6814.nh3'] = gas_data.nh3

        #fvalent
        data = pms5003.read()
        influx_json_prototyp[0]['fields']['pms5003.pm010'] = data.pm_ug_per_m3(1.0)
        influx_json_prototyp[0]['fields']['pms5003.pm025'] = data.pm_ug_per_m3(2.5)
        influx_json_prototyp[0]['fields']['pms5003.pm100'] = data.pm_ug_per_m3(10)

        influx_json_prototyp[0]['time'] = current_time  # ??????????


        if iterations >= 3:
            print("Write points: {0}".format(influx_json_prototyp))
            influx.write_points(influx_json_prototyp, time_precision='ms')
        else:
            print("Skip iteration: " + str(iterations))

        time.sleep(60)
        iterations += 1

# Exit cleanly
except KeyboardInterrupt:
    sys.exit(0)
