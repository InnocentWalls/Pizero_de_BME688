#!/usr/bin/env python3
"""
Raspberry Pi Zero W + Enviro+ センサーロガー
InfluxDBへの堅牢なデータ送信を実装
WiFi切断やネットワークエラーに対応し、1分間隔のロギングを厳密に保つ
"""

import time
from time import sleep
import math
import statistics
import logging
import sys
import socket
import traceback
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

try:
    # Pimoroni公式enviroplusライブラリのインポート
    from enviroplus import gas
    from enviroplus.noise import Noise
    # センサーライブラリ（enviroplusパッケージ内に統合されている場合と別パッケージの場合がある）
    try:
        # まずenviroplusパッケージ内からインポートを試みる（公式推奨）
        from enviroplus import BME280
        from enviroplus import LTR559
        from enviroplus import PMS5003
    except ImportError:
        # 別パッケージとして提供されている場合のフォールバック
        from bme280 import BME280
        from ltr559 import LTR559
        from pms5003 import PMS5003
except ImportError as e:
    print(f"警告: Enviro+ライブラリのインポートに失敗しました: {e}")
    print("必要なパッケージをインストールしてください:")
    print("  pip install enviroplus")
    print("または、Pimoroniの公式インストールスクリプトを使用:")
    print("  curl https://get.pimoroni.com/enviroplus | bash")
    print("別パッケージが必要な場合:")
    print("  pip install bme280 pms5003 ltr559")
    sys.exit(1)

try:
    from influxdb import InfluxDBClient
except ImportError:
    print("警告: InfluxDBクライアントのインポートに失敗しました")
    print("インストール方法: pip install influxdb")
    sys.exit(1)

# =============================
# 設定
# =============================
INFLUX_CONFIG = {
    "host": "192.168.100.7",
    "port": 8086,
    "username": "grafana",
    "password": "grafana",
    "database": "urban"
}

LOG_INTERVAL = 60  # 1分間隔（秒）- 厳密に保つ
MAX_RETRIES = 3  # 最大リトライ回数（短縮して次のサイクルに影響させない）
RETRY_DELAY = 2  # リトライ間隔（秒）- 短縮
CONNECTION_TIMEOUT = 5  # 接続タイムアウト（秒）- 短縮
NETWORK_CHECK_TIMEOUT = 2  # ネットワーク確認タイムアウト（秒）

# ログ設定
# /var/logへの書き込み権限がない場合、カレントディレクトリにログファイルを作成
LOG_FILE_PATH = '/var/log/sensor_logger.log'
try:
    # /var/logへの書き込みを試みる
    test_file = open(LOG_FILE_PATH, 'a')
    test_file.close()
except (PermissionError, OSError):
    # 書き込み権限がない場合は、カレントディレクトリに保存
    import os
    LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensor_logger.log')
    print(f"警告: /var/logへの書き込み権限がありません。ログファイルを {LOG_FILE_PATH} に保存します。")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# =============================
# ユーティリティ関数
# =============================
def check_internet_connection(host: str = "8.8.8.8", port: int = 53, timeout: int = None) -> bool:
    """
    インターネット接続を確認
    """
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


def check_influxdb_reachable(host: str, port: int, timeout: int = None) -> bool:
    """
    InfluxDBサーバーへのネットワーク接続を確認
    """
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


def check_influxdb_connection(client: InfluxDBClient) -> bool:
    """
    InfluxDBへの接続を確認
    """
    try:
        # ping()はタイムアウトが長い場合があるため、接続確認のみ
        # 実際の送信時にエラーが発生した場合は、その時点で再接続する
        return client is not None
    except Exception as e:
        logger.debug(f"InfluxDB接続確認失敗: {e}")
        return False


def create_influxdb_client() -> Optional[InfluxDBClient]:
    """
    InfluxDBクライアントを作成
    """
    try:
        # まずネットワーク接続を確認
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
        logger.info("InfluxDBクライアント作成成功")
        return client
    except Exception as e:
        logger.debug(f"InfluxDBクライアント作成エラー: {e}")
        return None


# センサーインスタンス（グローバル変数として一度だけ初期化）
_sensor_bme280 = None
_sensor_ltr559 = None
_sensor_pms5003 = None

# センサー読み取り安定化用の関数
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

def init_sensors():
    """センサーを初期化（一度だけ実行）"""
    global _sensor_bme280, _sensor_ltr559, _sensor_pms5003
    try:
        if _sensor_bme280 is None:
            _sensor_bme280 = BME280()
            logger.info("BME280センサー初期化成功")
            # 初期化直後に数回読み取って安定化
            for _ in range(3):
                try:
                    _sensor_bme280.get_temperature()
                    _sensor_bme280.get_humidity()
                    _sensor_bme280.get_pressure()
                    sleep(0.1)
                except Exception:
                    pass
        if _sensor_ltr559 is None:
            _sensor_ltr559 = LTR559()
            logger.info("LTR559センサー初期化成功")
        if _sensor_pms5003 is None:
            try:
                _sensor_pms5003 = PMS5003()
                logger.info("PMS5003センサー初期化成功")
            except Exception as e:
                logger.debug(f"PMS5003センサー初期化スキップ: {e}")
    except Exception as e:
        logger.error(f"センサー初期化エラー: {e}")
        raise

def read_sensor_data() -> Optional[Dict[str, Any]]:
    """
    センサーからデータを読み取る
    """
    global _sensor_bme280, _sensor_ltr559, _sensor_pms5003
    
    try:
        # センサーが初期化されていない場合は初期化
        if _sensor_bme280 is None or _sensor_ltr559 is None:
            init_sensors()
        
        # BME280（温度、湿度、気圧）- 中央値化で安定化
        raw_temp = median_read(_sensor_bme280.get_temperature, n=5)
        raw_hum = median_read(_sensor_bme280.get_humidity, n=5)
        raw_pres = median_read(_sensor_bme280.get_pressure, n=5)
        
        # スパイク対策（1分周期想定の変化量制限）
        temperature = sanitize("temperature", raw_temp, min_v=-20, max_v=60, max_step=0.5)
        humidity = sanitize("humidity", raw_hum, min_v=0, max_v=100, max_step=3.0)
        pressure = sanitize("pressure", raw_pres, min_v=800, max_v=1100, max_step=1.0)
        
        # ガスセンサー
        gas_data = gas.read_all()
        
        # LTR559（光センサー）
        lux = _sensor_ltr559.get_lux()
        proximity = _sensor_ltr559.get_proximity()
        
        # ノイズセンサー
        try:
            noise = Noise()
            noise_dba = round(noise.get_noise_profile()[3], 1)
        except Exception as e:
            logger.debug(f"ノイズセンサー読み取りスキップ: {e}")
            noise_dba = None
        
        # PMS5003（粒子状物質センサー）- オプション
        pm1 = None
        pm25 = None
        pm10 = None
        if _sensor_pms5003 is not None:
            try:
                pm_data = _sensor_pms5003.read()
                pm1 = pm_data.pm_ug_per_m3(1.0)
                pm25 = pm_data.pm_ug_per_m3(2.5)
                pm10 = pm_data.pm_ug_per_m3(10.0)
            except Exception as e:
                logger.debug(f"PMS5003読み取りスキップ: {e}")
        
        data = {
            "temperature": round(temperature, 2),
            "pressure": round(pressure, 2),
            "humidity": round(humidity, 2),
            "gas_oxidising": float(gas_data.oxidising),  # 1000で割らない
            "gas_reducing": float(gas_data.reducing),
            "gas_nh3": float(gas_data.nh3),
            "lux": float(lux),
            "proximity": proximity,
            "noise_dba": noise_dba
        }
        
        if pm1 is not None:
            data.update({
                "pm1": pm1,
                "pm25": pm25,
                "pm10": pm10
            })
        
        return data
        
    except Exception as e:
        logger.error(f"センサー読み取りエラー: {e}")
        logger.error(traceback.format_exc())
        return None


def send_to_influxdb(client: InfluxDBClient, data: Dict[str, Any]) -> bool:
    """
    InfluxDBにデータを送信（scd41/test.pyの形式に合わせる）
    """
    try:
        timestamp = datetime.utcnow().isoformat()
        HOST_TAG = "pi-living"  # デバイスタグ
        
        json_body = []
        
        # 温度
        if data.get("temperature") is not None:
            json_body.append({
                "measurement": "temperature",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(data.get("temperature"), 2)}
            })
        
        # 湿度
        if data.get("humidity") is not None:
            json_body.append({
                "measurement": "humidity",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(data.get("humidity"), 2)}
            })
        
        # 気圧
        if data.get("pressure") is not None:
            json_body.append({
                "measurement": "pressure",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(data.get("pressure"), 2)}
            })
        
        # PMS5003データ
        if data.get("pm1") is not None:
            json_body.append({
                "measurement": "pm1",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("pm1"))}
            })
            json_body.append({
                "measurement": "pm2_5",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("pm25"))}
            })
            json_body.append({
                "measurement": "pm10",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("pm10"))}
            })
        
        # ガスセンサー（1000で割らない、元の値のまま）
        if data.get("gas_oxidising") is not None:
            json_body.append({
                "measurement": "oxidising",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("gas_oxidising", 0) * 1000)}  # 元の値に戻す
            })
            json_body.append({
                "measurement": "reducing",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("gas_reducing", 0) * 1000)}
            })
            json_body.append({
                "measurement": "nh3",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("gas_nh3", 0) * 1000)}
            })
        
        # 照度
        if data.get("lux") is not None:
            json_body.append({
                "measurement": "lux",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": float(data.get("lux"))}
            })
        
        # ノイズ（noise_dbaがある場合）
        if data.get("noise_dba") is not None:
            json_body.append({
                "measurement": "noise_dba",
                "tags": {"device": HOST_TAG},
                "time": timestamp,
                "fields": {"value": round(data.get("noise_dba"), 1)}
            })
        
        if not json_body:
            logger.warning("送信するデータがありません")
            return False
        
        result = client.write_points(json_body)
        if result:
            logger.info(f"データ送信成功: {len(json_body)}ポイント")
            return True
        else:
            logger.warning("データ送信失敗（結果がFalse）")
            return False
            
    except Exception as e:
        # 接続エラーの場合は、クライアントを無効化する必要がある
        error_msg = str(e).lower()
        if "connection" in error_msg or "timeout" in error_msg or "network" in error_msg:
            logger.debug(f"InfluxDB送信エラー（接続関連）: {e}")
        else:
            logger.warning(f"InfluxDB送信エラー: {e}")
        return False


def send_with_retry(client: Optional[InfluxDBClient], data: Dict[str, Any], max_time: float = None) -> Tuple[bool, Optional[InfluxDBClient]]:
    """
    リトライロジック付きでInfluxDBに送信
    戻り値: (成功フラグ, クライアント)
    max_timeが指定されている場合、その時間内に収める
    """
    start_time = time.time()
    
    if client is None:
        logger.debug("InfluxDBクライアントがNoneのため、再接続を試みます")
        client = create_influxdb_client()
        if client is None:
            return False, None
    
    for attempt in range(MAX_RETRIES):
        # 時間制限チェック
        if max_time is not None:
            elapsed = time.time() - start_time
            if elapsed >= max_time:
                logger.debug(f"送信タイムアウト（{elapsed:.1f}秒経過）")
                return False, client
        
        try:
            # データ送信
            if send_to_influxdb(client, data):
                return True, client
            else:
                # 送信失敗時はクライアントを無効化して再接続を試みる
                client = None
                if attempt < MAX_RETRIES - 1:
                    # 残り時間を考慮して待機
                    if max_time is not None:
                        elapsed = time.time() - start_time
                        remaining = max_time - elapsed
                        if remaining > RETRY_DELAY:
                            sleep(RETRY_DELAY)
                        elif remaining > 0:
                            sleep(remaining)
                    else:
                        sleep(RETRY_DELAY)
                    
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


def main_loop():
    """
    メインループ
    1分間隔のロギングを厳密に保つ
    """
    logger.info("センサーロガーを開始します")
    logger.info(f"ロギング間隔: {LOG_INTERVAL}秒")
    logger.info(f"InfluxDB: {INFLUX_CONFIG['host']}:{INFLUX_CONFIG['port']}")
    
    # センサーを初期化（一度だけ）
    try:
        init_sensors()
    except Exception as e:
        logger.error(f"センサー初期化に失敗しました: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    client = None
    consecutive_failures = 0
    max_consecutive_failures = 20  # 連続失敗の許容回数を増やす（WiFi切断時に対応）
    
    # 次のロギング時刻を記録（厳密な間隔管理のため）
    next_log_time = time.time()
    
    while True:
        try:
            cycle_start_time = time.time()
            
            # ネットワーク接続確認（非ブロッキング的に短時間で確認）
            network_ok = check_internet_connection()
            influxdb_reachable = check_influxdb_reachable(INFLUX_CONFIG["host"], INFLUX_CONFIG["port"])
            
            if not network_ok or not influxdb_reachable:
                logger.debug(f"ネットワーク接続不良: internet={network_ok}, influxdb={influxdb_reachable}")
                # クライアントを無効化
                client = None
            else:
                # InfluxDBクライアントの確認・作成
                if client is None:
                    logger.debug("InfluxDBクライアントを作成します...")
                    client = create_influxdb_client()
                    if client is None:
                        logger.debug("InfluxDB接続失敗")
            
            # センサーデータ読み取り（ネットワーク状態に関係なく実行）
            sensor_data = read_sensor_data()
            if sensor_data is None:
                logger.warning("センサーデータの読み取りに失敗しました")
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    logger.error("連続失敗回数が上限に達しました。センサーを確認してください。")
                    consecutive_failures = 0
            else:
                # InfluxDBに送信（可能な場合）
                if client is not None:
                    # 送信に使える時間を計算（次のサイクルまでに余裕を持たせる）
                    elapsed = time.time() - cycle_start_time
                    max_send_time = LOG_INTERVAL - elapsed - 5  # 5秒のマージン
                    
                    if max_send_time > 0:
                        success, client = send_with_retry(client, sensor_data, max_time=max_send_time)
                        if success:
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            logger.debug(f"送信失敗（連続失敗回数: {consecutive_failures}）")
                    else:
                        logger.debug("送信時間が不足しているためスキップ")
                        consecutive_failures += 1
                else:
                    logger.debug("InfluxDBクライアントが利用できないため送信をスキップ")
                    consecutive_failures += 1
            
            # 次のロギング時刻まで正確に待機
            current_time = time.time()
            elapsed = current_time - cycle_start_time
            sleep_time = LOG_INTERVAL - elapsed
            
            if sleep_time > 0:
                sleep(sleep_time)
            else:
                # 処理に時間がかかりすぎた場合の警告
                logger.warning(f"サイクル処理が {abs(sleep_time):.1f}秒 遅延しました")
            
            # 次のロギング時刻を更新
            next_log_time = time.time()
            
        except KeyboardInterrupt:
            logger.info("ユーザーによる中断")
            break
        except Exception as e:
            logger.error(f"予期しないエラー: {e}")
            logger.error(traceback.format_exc())
            consecutive_failures += 1
            
            # エラー後も次のサイクル時刻を保つ
            current_time = time.time()
            elapsed = current_time - cycle_start_time
            sleep_time = LOG_INTERVAL - elapsed
            
            if sleep_time > 0:
                sleep(sleep_time)
            else:
                sleep(1)  # 最小待機時間


if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        logger.critical(f"致命的なエラー: {e}")
        logger.critical(traceback.format_exc())
        sys.exit(1)

