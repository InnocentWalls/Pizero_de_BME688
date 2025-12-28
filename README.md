# Enviro+ センサーロガー

Raspberry Pi Zero WとPimoroniのEnviro+を使用して、BME280などのセンサーから気温・湿度・気圧などの環境データを取得し、InfluxDBに送信するプロジェクトです。

## 特徴

- **堅牢なネットワーク処理**: WiFi切断やネットワークエラーに強く、自動的に再試行・再接続を行います
- **データ損失防止**: 送信失敗時にデータをローカルに保存し、接続復旧時に自動的に再送信します
- **センサーデータの安定化**: 
  - BME280の読み取りを中央値化してノイズを低減
  - CPU温度による補正を実装
  - スパイク（急激な変化）を検出して除外
- **1分間隔の厳密なロギング**: 処理時間を考慮して正確な間隔を維持

## 必要なハードウェア

- Raspberry Pi Zero W（または互換ボード）
- Pimoroni Enviro+ ボード
- PMS5003 粒子状物質センサー（オプション）

## 必要なソフトウェア

### Pythonパッケージ

```bash
pip install enviroplus pms5003 bme280 ltr559 influxdb
```

### システムパッケージ

```bash
sudo apt-get update
sudo apt-get install python3-pip python3-smbus i2c-tools
```

## セットアップ

### 1. リポジトリのクローン

```bash
git clone <repository-url>
cd scd41
```

### 2. 環境変数の設定

`.env.example`を`.env`にコピーして、実際の設定値を入力してください。

```bash
cp .env.example .env
nano .env
```

### 3. 環境変数の読み込み

システムの環境変数として設定するか、systemdサービスファイルで設定します。

#### 方法1: 環境変数として設定

```bash
export INFLUXDB_HOST=192.168.100.7
export INFLUXDB_PORT=8086
export INFLUXDB_USERNAME=grafana
export INFLUXDB_PASSWORD=grafana
export INFLUXDB_DATABASE=urban
export HOST_TAG=pi-living
```

#### 方法2: systemdサービスファイルで設定

`/etc/systemd/system/sensor-logger.service`を作成:

```ini
[Unit]
Description=Enviro+ Sensor Logger
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/scd41
Environment="INFLUXDB_HOST=192.168.100.7"
Environment="INFLUXDB_PORT=8086"
Environment="INFLUXDB_USERNAME=grafana"
Environment="INFLUXDB_PASSWORD=grafana"
Environment="INFLUXDB_DATABASE=urban"
Environment="HOST_TAG=pi-living"
ExecStart=/usr/bin/python3 /home/pi/scd41/sensor_logger.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4. I2Cの有効化

```bash
sudo raspi-config
# Interface Options > I2C > Enable
```

### 5. 実行権限の付与

```bash
chmod +x sensor_logger.py
```

### 6. ログディレクトリの作成（必要に応じて）

```bash
sudo mkdir -p /var/log
sudo chown pi:pi /var/log
```

## 使用方法

### 手動実行

```bash
python3 sensor_logger.py
```

### systemdサービスとして実行

```bash
# サービスを有効化
sudo systemctl enable sensor-logger.service

# サービスを開始
sudo systemctl start sensor-logger.service

# ステータス確認
sudo systemctl status sensor-logger.service

# ログ確認
sudo journalctl -u sensor-logger.service -f
```

## 設定項目

### 必須設定

- `INFLUXDB_HOST`: InfluxDBサーバーのホスト名またはIPアドレス
- `INFLUXDB_PORT`: InfluxDBサーバーのポート番号（デフォルト: 8086）
- `INFLUXDB_USERNAME`: InfluxDBのユーザー名
- `INFLUXDB_PASSWORD`: InfluxDBのパスワード
- `INFLUXDB_DATABASE`: InfluxDBのデータベース名

### オプション設定

- `HOST_TAG`: デバイス識別用のタグ（デフォルト: `raspberry-pi`）
- `LOG_INTERVAL_SEC`: ロギング間隔（秒）（デフォルト: 60）
- `MAX_RETRIES`: 最大リトライ回数（デフォルト: 3）
- `RETRY_DELAY_SEC`: リトライ間隔（秒）（デフォルト: 2）
- `CONNECTION_TIMEOUT_SEC`: 接続タイムアウト（秒）（デフォルト: 5）
- `NETWORK_CHECK_TIMEOUT_SEC`: ネットワーク確認タイムアウト（秒）（デフォルト: 2）
- `FAILED_DATA_FILE`: 失敗データ保存ファイルのパス（デフォルト: `/var/log/sensor_failed_data.json`）
- `MAX_FAILED_ENTRIES`: 最大保存エントリ数（デフォルト: 1000）
- `LOG_FILE`: ログファイルのパス（デフォルト: `/var/log/sensor_logger.log`）
- `LOG_LEVEL`: ログレベル（DEBUG, INFO, WARNING, ERROR）（デフォルト: INFO）

## 取得されるデータ

以下のセンサーデータがInfluxDBに送信されます:

- **temperature**: 気温（℃）- CPU温度で補正済み
- **humidity**: 湿度（%RH）
- **pressure**: 気圧（hPa）
- **pm1, pm2_5, pm10**: 粒子状物質濃度（μg/m³）- PMS5003使用時
- **oxidising, reducing, nh3**: ガスセンサー値
- **lux**: 照度（lux）
- **noise_dba**: 音圧レベル（dBA）

## データの保存と復旧

送信に失敗したデータは`FAILED_DATA_FILE`で指定されたファイルに保存されます。接続が復旧すると、自動的に保存されたデータが再送信されます。

失敗データファイルの場所を確認するには:

```bash
cat /var/log/sensor_failed_data.json
```

## トラブルシューティング

### センサーが読み取れない

```bash
# I2Cデバイスの確認
sudo i2cdetect -y 1

# ログの確認
tail -f /var/log/sensor_logger.log
```

### InfluxDBに接続できない

- ネットワーク接続を確認
- InfluxDBサーバーが起動しているか確認
- 認証情報が正しいか確認
- ファイアウォール設定を確認

### データが送信されない

- ログファイルを確認してエラーメッセージを確認
- 失敗データファイルにデータが保存されているか確認
- ネットワーク接続を確認

## ライセンス

このプロジェクトのライセンス情報を記載してください。

## 貢献

プルリクエストやイシューの報告を歓迎します。

