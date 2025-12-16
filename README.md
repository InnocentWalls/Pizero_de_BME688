Enviro+ & PMS5003 → InfluxDB 2.x Logger

Raspberry Pi + Pimoroni Enviro+ + PMS5003 の環境センサーデータを
InfluxDB 2.x に定期送信する Python スクリプト。

温度・湿度のスパイク（瞬間的な異常値）対策を重視した実装。

特徴

BME280（温度・湿度・気圧）

複数回読み取り → 中央値（median） を使用

前回値からの 変化量制限（スパイク除外）

CPU温度を使った ソフトな温度補正

PMS5003

PM1 / PM2.5 / PM10

MICS6814

酸化 / 還元 / NH3

LTR559

照度（lux）

ICS-43434

簡易 dBA 推定

InfluxDB 2.x

バッチ書き込み

自動リトライ（Wi-Fi 瞬断耐性）

計測・送信間隔

INTERVAL_SEC = 300

300秒（5分）間隔

変更する場合はこの値のみ変更

※ 間隔を短くする場合（例: 60秒）は
温度・湿度の max_step（変化量制限）も比例して下げること。

スパイク対策の考え方

前提：

センサーは たまに1回だけ異常値を返す

InfluxDB / Grafana はその1点を線で結ぶ

→ グラフ上に「針」が出る

対策：

中央値フィルタ

単発の異常値を除外

変化量制限（max_step）

物理的にありえない急変を破棄

None / NaN / Inf は送信しない

欠損値として扱う

デフォルトの変化量制限（5分周期想定）
センサー	制限
温度	±1.5 ℃ / 5分
湿度	±8 %RH / 5分
気圧	±3 hPa / 5分

※ 屋外設置・加湿器直下などでは調整が必要。

必要なハードウェア

Raspberry Pi

Pimoroni Enviro+

PMS5003

（任意）Enviro+ Noise（ICS-43434）

依存ライブラリ

influxdb-client

enviroplus

pms5003

（Raspberry Pi OS + Enviro+ 環境前提）

InfluxDB 設定

以下を自分の環境に合わせて変更すること。

INFLUX_URL

INFLUX_TOKEN

INFLUX_ORG

INFLUX_BUCKET

HOST_TAG

実行方法

python3 enviro_influx.py

常駐運用する場合は systemd または cron を推奨。

注意点

このコードは 「グラフを安定させる」ことを優先している

すべての瞬間値を忠実に記録したい用途には不向き

スパイク判定は「時間スケール × 変化量」を前提にしている

ライセンス

MIT License
（Enviro+ / PMS5003 各ライブラリのライセンスはそれぞれに従う）
