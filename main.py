#!/usr/bin/env python3
"""
Enviro+ ＆ PMS5003 を InfluxDB 2.x へ送信
* CPU 温度補正（CPU温度を平滑化）
* BME280読み取りを中央値化 + 外れ値/急変を除外（温度/湿度のスパイク対策）
* MEMS マイク (ICS-43434) で dBA 推定
* バッチ＋自動再試行で Wi-Fi 切断に強い
"""

from time import sleep
import math
import statistics
from collections import deque

from enviroplus import gas
from enviroplus.noise import Noise
from pms5003 import PMS5003
from bme280 import BME280
from ltr559 import LTR559

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import WriteOptions

# ── InfluxDB 接続設定 ──
INFLUX_URL    = "http://192.168.100.7:8086"
INFLUX_TOKEN  = "your-superlong-token-here"
INFLUX_ORG    = "your-org-name"
INFLUX_BUCKET = "urban"
HOST_TAG      = "pi-living"

# ── 計測周期 ──
INTERVAL_SEC = 300  # 5分

# ── CPU 温度補正 ──
def get_cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000.0  # °C

_cpu_hist = deque(maxlen=12)  # 直近12回(= 1時間ぶん/5分周期)から平均。必要なら短くしてOK

def compensate_temp(raw: float, factor: float = 5.0) -> float:
    cpu = get_cpu_temp()
    _cpu_hist.append(cpu)
    cpu_avg = sum(_cpu_hist) / len(_cpu_hist)
    return raw - (cpu_avg - raw) / factor

# ── センサー読み取り安定化 ──
def median_read(fn, n=5, sleep_s=0.05):
    """n回読んで中央値を返す（単発の変値を潰す）。全滅ならNone。"""
    vals = []
    for _ in range(n):
        try:
            v = fn()
            if v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                vals.append(v)
        except Exception:
            pass
        sleep(sleep_s)
    return statistics.median(vals) if vals else None

_last_good = {}

def sanitize(name, new, *, min_v=None, max_v=None, max_step=None):
    """
    - 範囲外は捨てる
    - 前回からの変化が大きすぎたら捨てる（スパイク対策）
    - None/NaN/Infも捨てる
    """
    old = _last_good.get(name)

    if new is None:
        return old
    if isinstance(new, float) and (math.isnan(new) or math.isinf(new)):
        return old

    if min_v is not None and new < min_v:
        return old
    if max_v is not None and new > max_v:
        return old

    if old is not None and max_step is not None and abs(new - old) > max_step:
        return old

    _last_good[name] = new
    return new

# ── 初期化 ──
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)

write_api = client.write_api(write_options=WriteOptions(
    batch_size=500,
    flush_interval=10_000,
    jitter_interval=2_000,
    retry_interval=5_000,
    max_retries=5,
    max_retry_delay=30_000,
    exponential_base=2
))

pms   = PMS5003()
bme   = BME280()
ltr   = LTR559()
noise = Noise()

# ── ループ ──
while True:
    # --- BME280：複数回読んで中央値 ---
    raw_temp = median_read(bme.get_temperature, n=5)
    hum      = median_read(bme.get_humidity,    n=5)
    pres     = median_read(bme.get_pressure,    n=5)

    # CPU補正（rawが取れた時だけ）
    temp = None
    if raw_temp is not None:
        temp = compensate_temp(raw_temp, factor=5.0)

    # --- スパイク抑制（5分周期想定の変化量制限） ---
    # 温度：5分で±1.5℃以上は基本スパイク扱い（室内想定。屋外なら2.5とかに上げる）
    temp = sanitize("temperature", temp, min_v=-20, max_v=60, max_step=1.5)

    # 湿度：5分で±8%RH以上は基本スパイク扱い（加湿器直撃とかなら上げる）
    hum  = sanitize("humidity", hum,  min_v=0, max_v=100, max_step=8.0)

    # 気圧：5分で±3hPa以上は基本スパイク扱い
    pres = sanitize("pressure", pres, min_v=800, max_v=1100, max_step=3.0)

    # --- ほかのセンサー ---
    try:
        gas_data = gas.read_all()
    except Exception:
        gas_data = None

    try:
        pm = pms.read()
    except Exception:
        pm = None

    try:
        lux = float(ltr.get_lux())
    except Exception:
        lux = None
    lux = sanitize("lux", lux, min_v=0, max_v=200000, max_step=100000)

    try:
        noise_dba = round(noise.get_noise_profile()[3], 1)
    except Exception:
        noise_dba = None

    # --- Influx Point 作成（値がNoneのものは送らない） ---
    points = []

    if temp is not None:
        points.append(Point("temperature").tag("device", HOST_TAG).field("value", round(temp, 2)))
    if hum is not None:
        points.append(Point("humidity").tag("device", HOST_TAG).field("value", round(hum, 2)))
    if pres is not None:
        points.append(Point("pressure").tag("device", HOST_TAG).field("value", round(pres, 2)))

    if pm is not None:
        points.append(Point("pm1").tag("device", HOST_TAG).field("value", float(pm.pm_ug_per_m3(1.0))))
        points.append(Point("pm2_5").tag("device", HOST_TAG).field("value", float(pm.pm_ug_per_m3(2.5))))
        points.append(Point("pm10").tag("device", HOST_TAG).field("value", float(pm.pm_ug_per_m3(10))))

    if gas_data is not None:
        points.append(Point("oxidising").tag("device", HOST_TAG).field("value", float(gas_data.oxidising)))
        points.append(Point("reducing").tag("device", HOST_TAG).field("value", float(gas_data.reducing)))
        points.append(Point("nh3").tag("device", HOST_TAG).field("value", float(gas_data.nh3)))

    if lux is not None:
        points.append(Point("lux").tag("device", HOST_TAG).field("value", float(lux)))

    if noise_dba is not None:
        points.append(Point("noise_dba").tag("device", HOST_TAG).field("value", float(noise_dba)))

    if points:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)

    sleep(INTERVAL_SEC)
