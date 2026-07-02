# Mac MQTT ZS-8K Client

This package subscribes to the MQTT data uploaded by the YED-C100/Y100E DTU.

## Run

```bash
cd /Users/yunying/Documents/Infrared/mac_mqtt_zs8k_client
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install paho-mqtt
python3 mqtt_zs8k_client_mac.py
```

By default it subscribes to:

```text
mqtt.i-pv.cn:1884
topic: testup/+
```

It writes:

```text
zs8k_mqtt.jsonl
zs8k_mqtt.csv
csv/863434087141161.csv
csv/863434087141369.csv
```

The per-DTU CSV files are sampled once every 10 seconds by default.

To change the per-DTU CSV sample interval:

```bash
python3 mqtt_zs8k_client_mac.py --sample-interval 10
```

To open the dashboard on this Mac or the LAN:

```bash
cd /Users/yunying/Documents/Infrared
python3 tools/zs8k_csv_dashboard_server.py --host 0.0.0.0 --port 8765
```

Dashboard URLs:

```text
http://127.0.0.1:8765/
http://10.5.70.229:8765/
http://YunyingdeMac-mini.local:8765/
```

For one DTU only:

```bash
python3 mqtt_zs8k_client_mac.py --topic testup/863434087141369
```
