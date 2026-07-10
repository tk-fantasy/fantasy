"""为Home Assistant添加MQTT broker配置"""
import json
from pathlib import Path

config_file = Path(__file__).parent / ".storage" / "core.config_entries"

# 读取现有配置
with open(config_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

# 检查是否已有MQTT配置
mqtt_entry = None
for entry in data['data']['entries']:
    if entry['domain'] == 'mqtt':
        mqtt_entry = entry
        break

if mqtt_entry:
    # 更新端口为1884
    if mqtt_entry['data'].get('port') != 1884:
        mqtt_entry['data']['port'] = 1884
        
        # 保存配置
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"✓ MQTT端口已更新为1884 (原来是{mqtt_entry['data'].get('port', '未知')})")
    else:
        print("ℹ MQTT配置已存在且端口正确")
else:
    print("✗ 未找到MQTT配置,请通过HA UI添加")
