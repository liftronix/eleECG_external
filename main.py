import uasyncio as asyncio
import machine, gc, os, uos, time, logger, _thread
from machine import Pin, I2C, WDT, Timer

import sysmon
from core1_manager import core1_main, stop_core1
from shared_state import get_sensor_snapshot
from ota_manager import (
    get_local_version,
    apply_ota_if_pending,
    verify_ota_commit,
    check_and_download_ota
)

from uthingsboard.client import TBDeviceMqttClient
from ledblinker import LEDBlinker
from wifi_manager import WiFiManager
from sdcard_manager import SDCardManager
from datalogger import DataLogger
from laser_module import LaserModule
from config_loader import load_config

config = load_config()


'''
# 🛑 Safe Mode via GPIO14
safe_pin = Pin(14, Pin.IN, Pin.PULL_UP)
if not safe_pin.value():
    logger.warn("Safe Mode triggered — skipping OTA and main loop")
    import sys
    sys.exit()
'''

# Set the pin to HIGH
low_power = Pin(2, Pin.OUT)
low_power.value(1)


# 🖥 Display
from scaled_ui.oled_ui import OLED_UI
from scaled_ui.button_handler import ButtonHandler
import ssd1306

i2c = I2C(0, scl=Pin(5), sda=Pin(4))
try:
    oled = ssd1306.SSD1306_I2C(128, 64, i2c)
    oled.fill(0)
    oled.show()
except OSError as e:
    logger.error("OLED init error:", e)

ui = OLED_UI(oled, scale=2)
ui.show_message(f"ELE-ECG\n{get_local_version()}")

# 🔆 LED Setup
led = Pin('LED', Pin.OUT)
led.value(0)

online_lock = asyncio.Event()
online_lock.clear() #Disconnected at start

# 📶 Wi-Fi Manager
wifi = WiFiManager(
    ssid=config.get("wifi", {}).get("ssid", ""),
    password=config.get("wifi", {}).get("password", "")
)
wifi.start(online_lock)

ota_lock = asyncio.Event()
ota_lock.set()  # Start with sensors enabled

# 🕒 REPL-safe boot delay
print("⏳ Boot delay... press Stop in Thonny to break into REPL")
time.sleep(3)

# Global counter for seconds
uptime_s = 0

def tick(timer):
    global uptime_s
    uptime_s += 1  # Timer fires every 1s

# Initialize the timer to call tick() every 1s
t = Timer()
t.init(mode=Timer.PERIODIC, period=1000, callback=tick)

def get_uptime():
    return uptime_s

'''
# Initialize watchdog with 8000ms timeout
wdt = WDT(timeout=8000)

async def wdt_feeder():
    while True:
        wdt.feed()
        await asyncio.sleep(2)
'''

# 🧮 Config Sync
def sync_config_if_changed(sd_path="/sd/config.json", flash_path="/config.json", file_name="config.json"):
    try:
        if file_name not in os.listdir("/sd"):
            logger.warn("No config.json found on SD card")
            return

        with open(sd_path, "rb") as f_sd:
            sd_data = f_sd.read()
        try:
            with open(flash_path, "rb") as f_flash:
                flash_data = f_flash.read()
        except OSError:
            flash_data = b""

        if sd_data != flash_data:
            with open(flash_path, "wb") as f:
                f.write(sd_data)
            logger.info("Updated config.json from SD card")
        else:
            logger.info("config.json is already up to date")
    except Exception as e:
        logger.error(f"Failed to sync config.json: {e}")


latest_sensor_data = {}
latest_sensor_lock = asyncio.Lock()

# 📤 Sensor Polling & Logging
async def drain_sensor_data(datalogger, ota_lock):
    last_seq = {}
    while True:
        await ota_lock.wait()  # ⛔ Block if OTA is active
        snapshot = get_sensor_snapshot()
        if not snapshot or "payload" not in snapshot or "seq" not in snapshot:
            await asyncio.sleep_ms(100)
            continue

        seqs = snapshot["seq"]
        payloads = snapshot["payload"]

        for sensor, data in payloads.items():
            current_seq = seqs.get(sensor, -1)
            previous_seq = last_seq.get(sensor, -2)

            if current_seq != previous_seq:
                last_seq[sensor] = current_seq

                # Update global structure
                async with latest_sensor_lock:
                    latest_sensor_data[sensor] = {
                        'seq': current_seq,
                        'value': data,
                        'timestamp': time.ticks_ms()
                    }

                entry = f"[{sensor}] Seq={current_seq} → {data}"
                logger.debug(entry)
                await datalogger.log(entry)
        await asyncio.sleep_ms(1000)

# 🔦Laser Polling
async def drain_laser_data(laser, snapshot_ref, datalogger, ota_lock):
    while True:
        await ota_lock.wait()  # ⛔ Block if OTA is active
        try:
            snapshot = await laser.measure_and_log(tag="laser")
            snapshot_ref.clear()
            snapshot_ref.update(snapshot)

            seq = snapshot["seq"].get("laser")
            value = snapshot["payload"].get("laser")

            # Update global structure
            async with latest_sensor_lock:
                latest_sensor_data["laser"] = {
                    'seq': seq,
                    'value': {
                        'disp_data': value,  # Wrap in dict
                        'distance': value,
                        'sensor': 'laser'
                    },
                    'timestamp': time.ticks_ms()
                }

            entry = f"[laser] Seq={seq} → {value} mm"
            await datalogger.log(entry)

        except Exception as e:
            logger.warn(f"Laser: Polling error — {e}")
        await asyncio.sleep_ms(1000)


# MQTT Publish
mqtt_seq_counter = 0
device_uptime = 0
async def send_to_thingsboard(client, ota_lock, ui):
    global mqtt_seq_counter
    while True:
        try:
            await asyncio.wait_for(online_lock.wait(), timeout=20)
            await ota_lock.wait()  # ⛔ Block if OTA is active
            try:
                client.connect()
                mqtt_seq_counter += 1
                ui.show_message(f"MQTT\n{mqtt_seq_counter}")

                # Read global snapshot safely
                async with latest_sensor_lock:
                    snapshot = latest_sensor_data.copy()
                
                local_time = time.localtime()
                l_date = "{:04d}-{:02d}-{:02d}".format(
                    local_time[0], local_time[1], local_time[2]
                )
                l_time = "{:02d}:{:02d}:{:02d}".format(
                    local_time[3], local_time[4], local_time[5]
                )

                # Package Telemetry Data
                payload = {
                    'Seq': str(mqtt_seq_counter),
                    'FW_Version': f"{get_local_version()}",
                    'device_uptime':f"{get_uptime()} sec",
                    'device_date': l_date,
                    'device_time': l_time
                }

                for sensor, data in snapshot.items():
                    value_dict = data.get("value", {})
                    disp_data = value_dict.get("disp_data")
                    
                    if disp_data is not None:
                        payload[f"{sensor}_value"] = disp_data
                    else:
                        # Optional: log warning and include fallback
                        logger.warn(f"{sensor}: disp_data missing. Sending error or raw value.")
                        payload[f"{sensor}_value"] = value_dict.get("error", str(value_dict))

                client.send_telemetry(payload, qos=1)
                logger.info(f"📤 Telemetry sent: {payload}")
                client.disconnect()
            except Exception as e:
                logger.error(f"⚠️ MQTT Publish Error: {e}")
        
        except asyncio.TimeoutError:
            logger.warn("⏳ Online check timed out — skipping telemetry this round")
            
        publish_interval = int(config.get("mqtt").get("publish_interval_sec"))
        await asyncio.sleep(max(5, publish_interval))


# Display Functions
async def get_sensor_display_functions():
    funcs = []
    async with latest_sensor_lock:
        snapshot = latest_sensor_data.copy()
        #print("📊 Live Sensor Snapshot:", snapshot)

    for sensor in snapshot:
        async def display_fn(name=sensor):
            async with latest_sensor_lock:
                data = latest_sensor_data.get(name)
            return f"{name}:\n{data['value']['disp_data']}" if data else f"{name}:\n--"
        funcs.append(display_fn)

    return funcs


async def refresh_ui_sources(ui, online_lock):
    while True:
        if online_lock.is_set():  # ⛔ Device is online, skip UI updates
            await asyncio.sleep(1)
            continue

        ui.sensors = await get_sensor_display_functions()
        logger.info(f"🔄 UI Sensor Functions Updated: {len(ui.sensors)}")
        await asyncio.sleep(1)


async def auto_refresh_ui(ui, online_lock, interval=2):
    while True:
        if online_lock.is_set():  # ⛔ Connected mode, skip display refresh
            
            logger.debug("UI → Skipped refresh (device is online)")
            await asyncio.sleep(interval)
            continue

        logger.debug("UI → auto_refresh_ui(): Triggering display refresh")
        await ui._render_current()
        await asyncio.sleep(interval)


# 🚀 Main Entry Point
async def main():    
    logger.info(f"🧾 Running firmware version: {get_local_version()}")
    
    # Start watchdog feeder as a background task
    #asyncio.create_task(wdt_feeder())
    
    asyncio.create_task(sysmon.idle_task())          # Track idle time
    asyncio.create_task(sysmon.monitor_resources())  # Start diagnostics
    
    led_blinker = LEDBlinker(pin_num='LED', interval_ms=200)
    led_blinker.start()
    
    # OTA 
    ui = OLED_UI(oled, scale=2)
    await apply_ota_if_pending(led)
    await verify_ota_commit(online_lock, ota_lock, ui)
    
    asyncio.create_task(check_and_download_ota(led, ota_lock, ui, online_lock))
    
    # SD Card and Data Logger
    sd = SDCardManager()
    await sd.mount()
    sync_config_if_changed()
    asyncio.create_task(sd.auto_manage())

    datalogger = DataLogger(sd, buffer_size=10, flush_interval_s=5)
    asyncio.create_task(datalogger.run())
    asyncio.create_task(drain_sensor_data(datalogger, ota_lock))
    
    # Laser
    laser = LaserModule()
    laser_snapshot = {}  # Shared container for latest laser data
    if not await laser.power_on():
        logger.error("Laser: Initialization failed")
    else:
        await laser.get_status()
        asyncio.create_task(drain_laser_data(laser, laser_snapshot, datalogger, ota_lock))
    
    # Core 1 sensors
    _thread.start_new_thread(core1_main, ())
    logger.info("🟢 Core 1 sensor sampling started.")
    
    #UI-Display
    sensor_display_fns = await get_sensor_display_functions()
    ui = OLED_UI(oled, sensor_display_fns, scale=2)
    asyncio.create_task(refresh_ui_sources(ui, online_lock))
    asyncio.create_task(auto_refresh_ui(ui, online_lock))
    await ui.next()
    
    # UI-button
    buttons = ButtonHandler(pin_left=6, pin_right=3)
    buttons.attach_ui(ui)
    buttons.start()
    
    #MQTT Initialization
    mqttHost = config.get("mqtt").get("host")
    mqttKey = config.get("mqtt").get("key")
    client = TBDeviceMqttClient(mqttHost, access_token = mqttKey)
    asyncio.create_task(send_to_thingsboard(client, ota_lock, ui))
    
    while True:
        status = wifi.get_status()
        print(f"WiFi Status: {status['WiFi']}, Internet: {status['Internet']}")
        print(f"IP Address: {wifi.get_ip_address()}")
       
        if not ota_lock.is_set():
            logger.debug("📴 Sensor paused due to OTA activity")
        
        await asyncio.sleep(10)

# 🧹 Graceful Shutdown
try:
    asyncio.run(main())
except KeyboardInterrupt:
    logger.info("🔻 Ctrl+C detected — shutting down...")
    stop_core1()
    time.sleep(1)
    logger.info("🛑 System shutdown complete.")

