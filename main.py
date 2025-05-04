import asyncore
import threading
import time
import base64
import requests
import logging
import paho.mqtt.client as mqtt
import json
import traceback

# Configure more detailed logging
logging.basicConfig(
    level=logging.DEBUG,  # Change to DEBUG for most verbose logging
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load config from Home Assistant's options.json file
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
alarm_send_delay_minutes = config.get('alarm_delay_minutes', 5)
alarm_send_delay = alarm_send_delay_minutes * 60

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

last_alarm_sent_time = 0

# In-memory alarm tracking
alarms = {}

def send_mqtt(topic):
    try:
        client = mqtt.Client("P1")
        client.username_pw_set(username=mqtt_username, password=mqtt_password)
        client.connect(broker_address)
        client.publish(str(topic), "1")
        logger.info(f"MQTT Published to topic '{topic}'")
    except Exception as e:
        logger.error(f"MQTT error: {e}")
        logger.error(traceback.format_exc())

def send_http_request(credentials, url, method, request_body, timeout):
    logger.debug(f"Sending HTTP request to {url}")
    logger.debug(f"Request method: {method}")
    logger.debug(f"Request body: {request_body}")

    base64_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {base64_credentials}'
    }

    try:
        if method.upper() == 'POST':
            logger.debug("Attempting to send POST request")
            response = requests.post(url, headers=headers, json=request_body, timeout=timeout)
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response content: {response.text}")
        else:
            raise ValueError("Only POST method is supported for SMS.")
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP SMS error: {e}")
        logger.error(f"Full error details: {traceback.format_exc()}")
        logger.error(f"Request details: URL={url}, Method={method}, Headers={headers}, Body={request_body}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in send_http_request: {e}")
        logger.error(traceback.format_exc())
        return None

def send_sms(message, number):
    logger.info(f"Attempting to send SMS to {number}")
    
    # Validate input parameters
    if not sms_uri:
        logger.error("SMS URI is not configured")
        return
    
    if not sms_credentials:
        logger.error("SMS credentials are not configured")
        return
    
    body = {
        "to": number,
        "content": message
    }
    
    try:
        result = send_http_request(sms_credentials, sms_uri, 'POST', body, 100)
        
        if result:
            logger.info(f"HTTP SMS sent successfully to {number}")
            logger.debug(f"SMS Send Result: {result}")
        else:
            logger.warning(f"Failed to send HTTP SMS to {number}")
    except Exception as e:
        logger.error(f"Exception in send_sms for number {number}: {e}")
        logger.error(traceback.format_exc())

def assign_to_memory(TypeOfAlarm, sensorid, Gatewayid, value, alarm_time):
    global last_alarm_sent_time

    current_time = time.time()
    if current_time - last_alarm_sent_time < alarm_send_delay:
        logger.info(f"Delaying alarm send due to cooldown. Current time: {current_time}, Last alarm time: {last_alarm_sent_time}")
        return

    last_alarm_sent_time = current_time

    if sensorid not in alarms:
        alarms[sensorid] = {
            'type': TypeOfAlarm,
            'gatewayid': Gatewayid,
            'value': value,
            'time': alarm_time,
            'sendstatus': "Not Sent"
        }

        alarm_message = (
            f' Alarm Alert! \\n'
            f'Alarm Case: {TypeOfAlarm} \\n'
            f'Sensor Id: {sensorid} \\n'
            f'Value: {value} \\n'
            f'Time: {alarm_time} \\n'
        )
        
        logger.info(f"Preparing to send alarm message: {alarm_message}")
        
        if not phone_numbers:
            logger.error("No phone numbers configured to send SMS")
            return

        for num in phone_numbers:
            logger.info(f"Attempting to send SMS to {num}")
            send_sms(alarm_message, num)
        
        if sensorid in cold_room_sensors:
            threading.Timer(5 * 60, drop_row, args=(sensorid,)).start()
        elif sensorid in normal_room_sensors:
            threading.Timer(5 * 60, drop_row, args=(sensorid,)).start()

def convertdata(s):
    try:
        s = s.replace("\n", "").replace("b'", "").replace("\n\n'", "")
        outlist = s.split("}")
        TypeOfAlarm = outlist[0].split(',')[0].split(":")[1].replace("\"", "")
        sensorid = outlist[4].split(',')[-1].split(":")[1].replace("\"", "")
        Gatewayid = outlist[4].split(',')[-2].split(":")[2].replace("\"", "")
        value = outlist[5].split(',')[4].replace("]]", "")
        alarm_time = outlist[5].split(',')[3].replace("]]", "").replace("[[", "").split("\"")[3]
        assign_to_memory(TypeOfAlarm, sensorid, Gatewayid, value, alarm_time)
    except Exception as e:
        logger.error(f"Error converting data: {e}")

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

server = EchoServer('', 5060)
asyncore.loop()
