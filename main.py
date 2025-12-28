#!/usr/bin/env python3
"""
Enviro+ ＆ PMS5003 を InfluxDB 1.x へ送信
* CPU 温度補正（CPU温度を平滑化）
* BME280読み取りを中央値化 + 外れ値/急変を除外（温度/湿度のスパイク対策）
* MEMS マイク (ICS-43434) で dBA 推定
* WiFi切断やネットワークエラーに強い堅牢な実装
* 1分間隔の厳密なロギング
* 送信失敗データの自動保存と復旧時の再送信
"""

import time
import math
import statistics
import logging
import socket
import traceback
import json
import os
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

from enviroplus import gas
from enviroplus.noise import Noise
from pms5003 import PMS5003
from bme280 import BME280
from ltr559 import LTR559

from influxdb import InfluxDBClient

# ── InfluxDB 接続設定（環境変数から読み込み） ──
INFLUX_CONFIG = {
    "host": os.getenv("INFLUXDB_HOST", "localhost"),
    "port": int(os.getenv("INFLUXDB_PORT", "8086")),
    "username": os.getenv("INFLUXDB_USERNAME", ""),
    "password": os.getenv("INFLUXDB_PASSWORD", ""),
    "database": os.getenv("INFLUXDB_DATABASE", "sensors")
}
HOST_TAG = os.getenv("HOST_TAG", "raspberry-pi")

# ── 計測周期 ──
INTERVAL_SEC = int(os.getenv("LOG_INTERVAL_SEC", "60"))  # デフォルト1分

# ── ネットワーク設定 ──
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))  # 最大リトライ回数
RETRY_DELAY = int(os.getenv("RETRY_DELAY_SEC", "2"))  # リトライ間隔（秒）
CONNECTION_TIMEOUT = int(os.getenv("CONNECTION_TIMEOUT_SEC", "5"))  # 接続タイムアウト（秒）
NETWORK_CHECK_TIMEOUT = int(os.getenv("NETWORK_CHECK_TIMEOUT_SEC", "2"))  # ネットワーク確認タイムアウト（秒）

# ── 失敗データ保存設定 ──
FAILED_DATA_FILE = os.getenv("FAILED_DATA_FILE", "/var/log/sensor_failed_data.json")  # 失敗データ保存ファイル
MAX_FAILED_ENTRIES = int(os.getenv("MAX_FAILED_ENTRIES", "1000"))  # 最大保存エントリ数（メモリ保護のため）

# ── ログ設定 ──
LOG_FILE = os.getenv("LOG_FILE", "/var/log/sensor_logger.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── CPU 温度補正 ──
def get_cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000.0  # °C

_cpu_hist = deque(maxlen=60)  # 直近60回(= 1時間ぶん/1分周期)から平均

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
        time.sleep(sleep_s)
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

# ── ネットワーク確認関数 ──
def check_influxdb_reachable(host: str, port: int, timeout: int = None) -> bool:
    """InfluxDBサーバーへのネットワーク接続を確認"""
    if timeout is None:
        timeout = NETWORK_CHECK_TIMEOUT
    try:
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def create_influxdb_client() -> Optional[InfluxDBClient]:
    """InfluxDBクライアントを作成"""
    try:
        if not check_influxdb_reachable(INFLUX_CONFIG["host"], INFLUX_CONFIG["port"]):
            logger.debug(f"InfluxDBサーバー {INFLUX_CONFIG['host']}:{INFLUX_CONFIG['port']} に到達できません")
            return None
        
        client = InfluxDBClient(
            host=INFLUX_CONFIG["host"],
            port=INFLUX_CONFIG["port"],
            username=INFLUX_CONFIG["username"],
            password=INFLUX_CONFIG["password"],
            database=INFLUX_CONFIG["database"],
            timeout=CONNECTION_TIMEOUT
        )
        logger.debug("InfluxDBクライアント作成成功")
        return client
    except Exception as e:
        logger.debug(f"InfluxDBクライアント作成エラー: {e}")
        return None

def send_to_influxdb(client: InfluxDBClient, points_data: list) -> bool:
    """InfluxDBにデータを送信"""
    try:
        json_body = []
        for point in points_data:
            json_body.append({
                "measurement": point["measurement"],
                "tags": point.get("tags", {}),
                "time": point.get("time", datetime.utcnow().isoformat()),
                "fields": point["fields"]
            })
        
        result = client.write_points(json_body)
        if result:
            logger.info(f"データ送信成功: {len(json_body)}ポイント")
            return True
        else:
            logger.warning("データ送信失敗（結果がFalse）")
            return False
    except Exception as e:
        error_msg = str(e).lower()
        if "connection" in error_msg or "timeout" in error_msg or "network" in error_msg:
            logger.debug(f"InfluxDB送信エラー（接続関連）: {e}")
        else:
            logger.warning(f"InfluxDB送信エラー: {e}")
        return False

def send_with_retry(client: Optional[InfluxDBClient], points_data: list, max_time: float = None) -> Tuple[bool, Optional[InfluxDBClient]]:
    """リトライロジック付きでInfluxDBに送信"""
    start_time = time.time()
    
    if client is None:
        logger.debug("InfluxDBクライアントがNoneのため、再接続を試みます")
        client = create_influxdb_client()
        if client is None:
            return False, None
    
    for attempt in range(MAX_RETRIES):
        if max_time is not None:
            elapsed = time.time() - start_time
            if elapsed >= max_time:
                logger.debug(f"送信タイムアウト（{elapsed:.1f}秒経過）")
                return False, client
        
        try:
            if send_to_influxdb(client, points_data):
                return True, client
            else:
                client = None
                if attempt < MAX_RETRIES - 1:
                    if max_time is not None:
                        elapsed = time.time() - start_time
                        remaining = max_time - elapsed
                        if remaining > RETRY_DELAY:
                            time.sleep(RETRY_DELAY)
                        elif remaining > 0:
                            time.sleep(remaining)
                    else:
                        time.sleep(RETRY_DELAY)
                    
                    logger.debug(f"送信失敗、再接続を試みます（試行 {attempt + 2}/{MAX_RETRIES}）")
                    client = create_influxdb_client()
                    if client is None:
                        continue
                else:
                    logger.debug("最大リトライ回数に達しました")
                    return False, None
        except Exception as e:
            logger.debug(f"送信試行エラー（試行 {attempt + 1}/{MAX_RETRIES}）: {e}")
            client = None
            if attempt < MAX_RETRIES - 1:
                if max_time is not None:
                    elapsed = time.time() - start_time
                    remaining = max_time - elapsed
                    if remaining > RETRY_DELAY:
                        time.sleep(RETRY_DELAY)
                    elif remaining > 0:
                        time.sleep(remaining)
                else:
                    time.sleep(RETRY_DELAY)
                client = create_influxdb_client()
            else:
                return False, None
    
    return False, client

# ── 失敗データ保存・復旧機能 ──
def save_failed_data(points_data: list, timestamp: str) -> None:
    """送信失敗データをファイルに保存"""
    try:
        failed_entry = {
            "timestamp": timestamp,
            "saved_at": datetime.utcnow().isoformat(),
            "points_data": points_data
        }
        
        # 既存データを読み込む
        failed_data = load_failed_data()
        
        # 新しいエントリを追加
        failed_data.append(failed_entry)
        
        # 最大エントリ数を超えた場合は古いものから削除
        if len(failed_data) > MAX_FAILED_ENTRIES:
            failed_data = failed_data[-MAX_FAILED_ENTRIES:]
            logger.warning(f"失敗データが最大数({MAX_FAILED_ENTRIES})に達しました。古いデータを削除します。")
        
        # ファイルに保存
        # ディレクトリが存在しない場合は作成
        file_dir = os.path.dirname(FAILED_DATA_FILE)
        if file_dir and not os.path.exists(file_dir):
            try:
                os.makedirs(file_dir, exist_ok=True)
            except Exception as e:
                logger.warning(f"ディレクトリ作成失敗: {e}。カレントディレクトリに保存します。")
                # カレントディレクトリに保存
                failed_file = "sensor_failed_data.json"
        else:
            failed_file = FAILED_DATA_FILE
        
        with open(failed_file, 'w', encoding='utf-8') as f:
            json.dump(failed_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"失敗データを保存しました: {len(points_data)}ポイント（合計{len(failed_data)}エントリ）")
    except Exception as e:
        logger.error(f"失敗データの保存エラー: {e}")
        logger.error(traceback.format_exc())

def load_failed_data() -> List[Dict[str, Any]]:
    """保存された失敗データを読み込む"""
    try:
        # まず通常のパスを試す
        if os.path.exists(FAILED_DATA_FILE):
            with open(FAILED_DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                else:
                    logger.warning("失敗データファイルの形式が不正です。空のリストを返します。")
                    return []
        # カレントディレクトリも確認
        elif os.path.exists("sensor_failed_data.json"):
            with open("sensor_failed_data.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                else:
                    logger.warning("失敗データファイルの形式が不正です。空のリストを返します。")
                    return []
        else:
            return []
    except json.JSONDecodeError as e:
        logger.warning(f"失敗データファイルのJSON解析エラー: {e}。空のリストを返します。")
        return []
    except Exception as e:
        logger.debug(f"失敗データの読み込みエラー: {e}")
        return []

def clear_failed_data() -> None:
    """保存された失敗データをクリア"""
    try:
        # 通常のパスを試す
        if os.path.exists(FAILED_DATA_FILE):
            os.remove(FAILED_DATA_FILE)
            logger.info("失敗データファイルをクリアしました")
        # カレントディレクトリも確認
        elif os.path.exists("sensor_failed_data.json"):
            os.remove("sensor_failed_data.json")
            logger.info("失敗データファイルをクリアしました")
    except Exception as e:
        logger.warning(f"失敗データファイルのクリアエラー: {e}")

def retry_failed_data(client: InfluxDBClient) -> Tuple[int, int]:
    """
    保存された失敗データを再送信
    戻り値: (成功数, 失敗数)
    """
    failed_data = load_failed_data()
    if not failed_data:
        return 0, 0
    
    logger.info(f"保存された失敗データを再送信します: {len(failed_data)}エントリ")
    
    success_count = 0
    fail_count = 0
    
    for idx, entry in enumerate(failed_data):
        try:
            points_data = entry.get("points_data", [])
            if not points_data:
                continue
            
            # バッチで送信（1エントリずつ）
            if send_to_influxdb(client, points_data):
                success_count += 1
                logger.debug(f"再送信成功 ({idx + 1}/{len(failed_data)}): {entry.get('timestamp', 'N/A')}")
            else:
                fail_count += 1
                logger.debug(f"再送信失敗 ({idx + 1}/{len(failed_data)}): {entry.get('timestamp', 'N/A')}")
                # 失敗した場合は残りのデータを保存して終了
                remaining_data = failed_data[idx:]
                try:
                    file_dir = os.path.dirname(FAILED_DATA_FILE)
                    if file_dir and not os.path.exists(file_dir):
                        failed_file = "sensor_failed_data.json"
                    else:
                        failed_file = FAILED_DATA_FILE
                    
                    with open(failed_file, 'w', encoding='utf-8') as f:
                        json.dump(remaining_data, f, ensure_ascii=False, indent=2)
                    logger.info(f"再送信が途中で失敗しました。残り{len(remaining_data)}エントリを保存しました。")
                except Exception as e:
                    logger.warning(f"残りデータの保存エラー: {e}")
                break
            
            # 送信間隔を少し空ける（サーバー負荷軽減）
            time.sleep(0.1)
            
        except Exception as e:
            logger.warning(f"再送信エラー ({idx + 1}/{len(failed_data)}): {e}")
            fail_count += 1
            # エラーが発生した場合も残りのデータを保存
            remaining_data = failed_data[idx:]
            try:
                file_dir = os.path.dirname(FAILED_DATA_FILE)
                if file_dir and not os.path.exists(file_dir):
                    failed_file = "sensor_failed_data.json"
                else:
                    failed_file = FAILED_DATA_FILE
                
                with open(failed_file, 'w', encoding='utf-8') as f:
                    json.dump(remaining_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            break
    
    # 全て成功した場合はファイルをクリア
    if fail_count == 0 and success_count > 0:
        clear_failed_data()
        logger.info(f"全ての失敗データの再送信が完了しました: {success_count}エントリ")
    elif success_count > 0:
        logger.info(f"失敗データの再送信を完了しました: 成功{success_count}、失敗{fail_count}")
    
    return success_count, fail_count

# ── センサー初期化 ──
try:
    pms = PMS5003()
    bme = BME280()
    ltr = LTR559()
    noise = Noise()
    logger.info("センサー初期化成功")
except Exception as e:
    logger.error(f"センサー初期化エラー: {e}")
    logger.error(traceback.format_exc())
    raise

# ── メインループ ──
logger.info("センサーロガーを開始します")
logger.info(f"ロギング間隔: {INTERVAL_SEC}秒")
logger.info(f"InfluxDB: {INFLUX_CONFIG['host']}:{INFLUX_CONFIG['port']}")
logger.info(f"データベース: {INFLUX_CONFIG['database']}")

client = None
next_log_time = time.time()

while True:
    try:
        cycle_start_time = time.time()
        
        # 接続状態を追跡（復旧検出のため）
        was_connected = (client is not None)
        
        # ネットワーク接続確認
        influxdb_reachable = check_influxdb_reachable(INFLUX_CONFIG["host"], INFLUX_CONFIG["port"])
        
        if not influxdb_reachable:
            logger.debug("InfluxDBサーバーに到達できません")
            client = None
        else:
            if client is None:
                logger.debug("InfluxDBクライアントを作成します...")
                client = create_influxdb_client()
                if client is None:
                    logger.debug("InfluxDB接続失敗")
                else:
                    # 接続が復旧した場合、保存された失敗データを再送信
                    if not was_connected:
                        logger.info("InfluxDB接続が復旧しました。保存された失敗データを再送信します...")
                        retry_success, retry_fail = retry_failed_data(client)
                        if retry_success > 0:
                            logger.info(f"再送信完了: 成功{retry_success}、失敗{retry_fail}")
        
        # --- BME280：複数回読んで中央値 ---
        raw_temp = median_read(bme.get_temperature, n=5)
        hum      = median_read(bme.get_humidity,    n=5)
        pres     = median_read(bme.get_pressure,    n=5)

        # CPU補正（rawが取れた時だけ）
        temp = None
        if raw_temp is not None:
            temp = compensate_temp(raw_temp, factor=5.0)

        # --- スパイク抑制（1分周期想定の変化量制限） ---
        temp = sanitize("temperature", temp, min_v=-20, max_v=60, max_step=0.5)
        hum  = sanitize("humidity", hum,  min_v=0, max_v=100, max_step=3.0)
        pres = sanitize("pressure", pres, min_v=800, max_v=1100, max_step=1.0)

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

        # --- InfluxDB Point 作成（値がNoneのものは送らない） ---
        points_data = []
        timestamp = datetime.utcnow().isoformat()

        if temp is not None:
            points_data.append({
                "measurement": "temperature",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(temp, 2)}
            })
        if hum is not None:
            points_data.append({
                "measurement": "humidity",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(hum, 2)}
            })
        if pres is not None:
            points_data.append({
                "measurement": "pressure",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(pres, 2)}
            })

        if pm is not None:
            points_data.append({
                "measurement": "pm1",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(pm.pm_ug_per_m3(1.0))}
            })
            points_data.append({
                "measurement": "pm2_5",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(pm.pm_ug_per_m3(2.5))}
            })
            points_data.append({
                "measurement": "pm10",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(pm.pm_ug_per_m3(10))}
            })

        if gas_data is not None:
            points_data.append({
                "measurement": "oxidising",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(gas_data.oxidising)}
            })
            points_data.append({
                "measurement": "reducing",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(gas_data.reducing)}
            })
            points_data.append({
                "measurement": "nh3",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(gas_data.nh3)}
            })

        if lux is not None:
            points_data.append({
                "measurement": "lux",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(lux)}
            })

        if noise_dba is not None:
            points_data.append({
                "measurement": "noise_dba",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(noise_dba)}
            })

        # InfluxDBに送信（可能な場合）
        if points_data and client is not None:
            elapsed = time.time() - cycle_start_time
            max_send_time = INTERVAL_SEC - elapsed - 5  # 5秒のマージン
            
            if max_send_time > 0:
                success, client = send_with_retry(client, points_data, max_time=max_send_time)
                if not success:
                    # 送信失敗時はデータを保存
                    logger.debug("データ送信失敗。データを保存します。")
                    save_failed_data(points_data, timestamp)
            else:
                logger.debug("送信時間が不足しているためスキップ")
                # 時間不足でもデータは保存
                save_failed_data(points_data, timestamp)
        elif points_data:
            # クライアントが利用できない場合もデータを保存
            logger.debug("InfluxDBクライアントが利用できないため送信をスキップ。データを保存します。")
            save_failed_data(points_data, timestamp)
        
        # 次のロギング時刻まで正確に待機
        current_time = time.time()
        elapsed = current_time - cycle_start_time
        sleep_time = INTERVAL_SEC - elapsed
        
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            logger.warning(f"サイクル処理が {abs(sleep_time):.1f}秒 遅延しました")
        
        next_log_time = time.time()
        
    except KeyboardInterrupt:
        logger.info("ユーザーによる中断")
        break
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        logger.error(traceback.format_exc())
        
        # エラー後も次のサイクル時刻を保つ
        current_time = time.time()
        elapsed = current_time - cycle_start_time
        sleep_time = INTERVAL_SEC - elapsed
        
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            time.sleep(1)  # 最小待機時間

