Enviro+ & PMS5003 → InfluxDB 2.x Logger

Raspberry Pi + Pimoroni Enviro+ + PMS5003 の環境センサーデータを
InfluxDB 2.x に定期送信する Python スクリプト。

温度・湿度のスパイク（瞬間的な異常値）対策を重視した実装になっている。

特徴

BME280（温度・湿度・気圧）

複数回読み取り → 中央値（median） を採用

前回値からの 変化量制限（スパイク除外）

CPU温度を使った ソフトな温度補正

PMS5003

PM1 / PM2.5 / PM10 を取得

MICS6814

酸化 / 還元 / NH3

LTR559

照度（lux）

ICS-43434

簡易 dBA 推定

InfluxDB 2.x

バッチ書き込み

自動リトライ（Wi-Fi瞬断耐性あり）

計測・送信間隔
INTERVAL_SEC = 300


300秒（5分）間隔

間隔を変更する場合は、この値を変更するだけ

⚠️ 間隔を短くする場合（例: 60秒）は
max_step（変化量制限）の閾値も比例して下げること。

スパイク対策の考え方

このコードでは以下を前提にしている：

センサーは たまに1回だけ変な値を返す

InfluxDB / Grafana はその1点をそのまま線で結ぶ

→ 見た目が「針」になる

対策として：

中央値フィルタ

単発の異常値を除外

変化量制限（max_step）

物理的にありえない急変を破棄

None / NaN / Inf は送信しない

欠損として扱う

デフォルトの変化量制限（5分周期想定）
センサー	制限内容
温度	±1.5 ℃ / 5分
湿度	±8 %RH / 5分
気圧	±3 hPa / 5分

※ 実環境（屋外・加湿器直下など）に応じて調整すること。

必要なハードウェア

Raspberry Pi

Pimoroni Enviro+

PMS5003
-（任意）Enviro+ Noise（ICS-43434）

依存ライブラリ
pip install influxdb-client
pip install enviroplus
pip install pms5003


（Raspberry Pi OS + Enviro+ 前提）

InfluxDB 設定

コード内の以下を自分の環境に合わせて変更する：

INFLUX_URL
INFLUX_TOKEN
INFLUX_ORG
INFLUX_BUCKET
HOST_TAG

実行
python3 enviro_influx.py


systemd や cron での常駐実行を推奨。

注意点

このコードは 「きれいなグラフを得る」ことを優先している
→ すべての瞬間値を忠実に記録したい用途には向かない

スパイクが「本当の環境変化」なのか「センサー異常」なのかは
変化量と時間スケールで判断する前提

ライセンス

MIT License
（Enviro+ / PMS5003 各ライブラリのライセンスはそれぞれに従う）
