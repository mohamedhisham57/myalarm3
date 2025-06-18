import asyncore
import binascii
import threading
import time
import base64
import requests
import logging
from queue import Queue
import json           
import traceback 
import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion

# === Logger Setup ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    with open('/data/options.json', 'r') as config_file:
        config = json.load(config_file)
except Exception as e:
    logger.error(f"Failed to load config file: {e}")
    logger.error(traceback.format_exc())
    config = {}  # Provide a default empty config to prevent further errors

# Extract configuration with error handling and logging
broker_address = config.get('mqtt_broker', '')
mqtt_username = config.get('mqtt_user', '')
mqtt_password = config.get('mqtt_pass', '')
sms_uri = config.get('sms_uri', '')
sms_credentials = config.get('sms_credentials', '')
cold_alarm_send_delay_minutes = config.get('cold_room_delay_minutes', 5)
normal_alarm_send_delay_minutes = config.get('normal_room_delay_minutes', 5)

cold_alarm_send_delay = cold_alarm_send_delay_minutes * 60
normal_alarm_send_delay = normal_alarm_send_delay_minutes * 60



# Parsing configuration with detailed logging
try:
    cold_room_sensors = [s.strip() for s in config.get('cold_room_sensors', '').split(',') if s.strip()]
    normal_room_sensors = [s.strip() for s in config.get('normal_room_sensors', '').split(',') if s.strip()]
    phone_numbers = [s.strip() for s in config.get('phone_numbers', '').split(',') if s.strip()]
    
    logger.info(f"Configured Cold Room Sensors: {cold_room_sensors}")
    logger.info(f"Configured Normal Room Sensors: {normal_room_sensors}")
    logger.info(f"Configured Phone Numbers: {phone_numbers}")
except Exception as e:
    logger.error(f"Error parsing sensor and phone number configuration: {e}")
    logger.error(traceback.format_exc())
    cold_room_sensors = []
    normal_room_sensors = []
    phone_numbers = []

def send_mqtt(topic):
    try:
        client = mqtt.Client(
            client_id="P1",
            protocol=mqtt.MQTTv311
        )

        logger.info(f"Sending MQTT message to topic: {topic}")
        client.username_pw_set(mqtt_username, mqtt_password)
        client.connect(broker_address)
        client.loop_start()
        client.publish(topic, "1")
        time.sleep(1)  # give time to complete the publish
        client.loop_stop()
        client.disconnect()
        logger.info("MQTT message sent successfully")
    except Exception as e:
        logger.error(f"Failed to send MQTT message: {e}")

# === Queue for alarms ===
alarm_queue = Queue()

# === SMS Sender ===
def send_http_request(credentials, url, method, request_body, timeout):
    base64_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {base64_credentials}'
    }
    try:
        if method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=request_body, timeout=timeout)
            response.raise_for_status()
            return response.json()
        else:
            raise ValueError("Unsupported HTTP method")
    except requests.RequestException as e:
        logger.error(f"HTTP error: {e}")
        return None

def send_sms(message, number):
    body = {"to": number, "content": message}
    result = send_http_request(sms_credentials, sms_uri, 'POST', body, 100)
    if result:
        logger.info(f"SMS sent to {number}")
    else:
        logger.warning(f"SMS failed to {number}")

# === Siren Worker ===
def siren_worker():
    last_sent_time = {}

    while True:
        sensor_id, temp, alarm_type, timestamp = alarm_queue.get()
        now = time.time()
        print("siren_worker is running...")

        if sensor_id in cold_room_sensors:
            delay = cold_alarm_send_delay
        elif sensor_id in normal_room_sensors:
            delay = normal_alarm_send_delay
        else:
            logger.warning(f"Unknown sensor type for sensor: {sensor_id}")
            alarm_queue.task_done()
            continue

        if now - last_sent_time.get(sensor_id, 0) < delay:
            logger.info(f"Cooldown active for {sensor_id}")
            alarm_queue.task_done()
            continue

        message = (
            f'There is Alarm \n'
            f"Alarm Case: {alarm_type}\n"
            f"Sensor Id: {sensor_id}\n"
            f"Value: {temp}\n"
            f"Time: {timestamp}"
        )
        for number in phone_numbers:
            send_sms(message, number)
            time.sleep(40)


        send_mqtt(alarm_type)  # either "cold_room" or "normal_room"
        last_sent_time[sensor_id] = now
        alarm_queue.task_done()


# === Data Handler ===
def convertdata(s):
    try:
        s = s.replace("\n", "").replace("b'", "").replace("\n\n'", "")
        outlist = s.split("}")
        TypeOfAlarm = outlist[0].split(',')[0].split(":")[1].replace("\"", "")
        sensorid = outlist[4].split(',')[-1].split(":")[1].replace("\"", "")
        Gatewayid = outlist[4].split(',')[-2].split(":")[2].replace("\"", "")
        value = outlist[5].split(',')[4].replace("]]", "")
        timestamp = outlist[5].split(',')[3].replace("]]", "").replace("[[", "").split("\"")[3]

        if sensorid in cold_room_sensors:
            alarm_type = "cold room"
        elif sensorid in normal_room_sensors:
            alarm_type = "normal room"
        else:
            logger.warning(f"Unknown sensor: {sensorid}")
            return

        alarm_queue.put((sensorid, value, alarm_type, timestamp))

    except Exception as e:
        logger.error(f"Failed to parse packet: {e}")

# === Async Server ===
class EchoHandler(asyncore.dispatcher_with_send):
    def handle_read(self):
        data = self.recv(8192)
        if data:
            convertdata(str(data))

class EchoServer(asyncore.dispatcher):
    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.create_socket()
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(5)

    def handle_accepted(self, sock, addr):
        logger.info(f"Incoming connection from {addr}")
        handler = EchoHandler(sock)

# === Start Worker and Server ===
threading.Thread(target=siren_worker, daemon=True).start()
logger.info("Starting alarm server on port 5060")
server = EchoServer('', 5060)
asyncore.loop()
