import asyncore
import base64
import logging
import os
import queue
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import requests

# Initialize the logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
MQTT_BROKER = "core-mosquitto"  # Home Assistant's MQTT broker service name
MQTT_USERNAME = "skarpt"
MQTT_PASSWORD = "Skarpt"
SMS_API_URL = "http://192.168.0.100:3000/api/v1/sms/outbox"
SMS_API_CREDENTIALS = "apiuser:pleasechangeme"
TCP_SERVER_HOST = ''
TCP_SERVER_PORT = 5060
SMS_DELAY = 40  # seconds between SMS sends

# Duration settings (in minutes)
COLD_ROOM_TIMEOUT = 30  # minutes
NORMAL_ROOM_TIMEOUT = 15  # minutes

# Hardcoded values
# List of sensor IDs for cold rooms
list_of_cold_room_sensors = ['62232132']

# List of sensor IDs for normal rooms
list_of_normal_room_sensors = ['62232133', '62232134']

# List of phone numbers to send SMS alerts to
phone_numbers = ['01140214856']
# Global variables
alarm_queue = queue.Queue()  # Queue to store alarms
sent_alarms = set()  # Set to track which alarms have been sent
alarm_timers = {}  # Dictionary to track active alarm timers

# Load sensor lists
def load_sensor_list(filepath):
    """Load sensor IDs from a file into a list."""
    try:
        if os.path.exists(filepath):
            with open(filepath, "r") as file:
                return ['62232132']
        else:
            logger.warning(f"Sensor list file not found: {filepath}")
            return ['62232132']
    except Exception as e:
        logger.error(f"Error loading sensor list from {filepath}: {e}")
        return ['62232132']

# Load phone numbers
def load_phone_numbers(filepath):
    """Load phone numbers from a file."""
    try:
        if os.path.exists(filepath):
            with open(filepath, "r") as file:
                return ['01140214856']
        else:
            logger.warning(f"Phone numbers file not found: {filepath}")
            return ['01140214856']
    except Exception as e:
        logger.error(f"Error loading phone numbers from {filepath}: {e}")
        return []

# Alarm object to store alarm data
class Alarm:
    def __init__(self, alarm_type, sensor_id, gateway_id, value, timestamp):
        self.alarm_type = alarm_type
        self.sensor_id = sensor_id
        self.gateway_id = gateway_id
        self.value = value
        self.timestamp = timestamp
        self.creation_time = time.time()
        self.sent = False
    
    def __str__(self):
        return (
            f'There is Alarm \n'
            f'Alarm Case: {self.alarm_type} \n'
            f'Sensor Id: {self.sensor_id} \n'
            f'Value: {self.value} \n'
            f'Time: {self.timestamp} \n'
        )
    
    def __eq__(self, other):
        if not isinstance(other, Alarm):
            return False
        return self.sensor_id == other.sensor_id
    
    def __hash__(self):
        return hash(self.sensor_id)

def send_http_request(credentials, url, method, request_body=None, timeout=30):
    """Send an HTTP request with basic authentication."""
    # Encode credentials in Base64
    base64_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')

    # Set up HTTP headers
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {base64_credentials}'
    }

    try:
        # Make the HTTP request based on the method
        if method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=request_body, timeout=timeout)
        elif method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'PUT':
            response = requests.put(url, headers=headers, json=request_body, timeout=timeout)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, headers=headers, timeout=timeout)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Check for errors in the response
        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"Error during HTTP request: {e}")
        return None

def send_sms(message, number):
    """Send an SMS via the API."""
    body = {
        "to": number,
        "content": message
    }

    try:
        result = send_http_request(SMS_API_CREDENTIALS, SMS_API_URL, 'POST', body, 100)
        if result:
            logger.info(f"SMS sent successfully to {number}")
            return True
        else:
            logger.error(f"Failed to send SMS to {number}")
            return False
    except Exception as e:
        logger.error(f"Exception while sending SMS: {e}")
        return False

def send_mqtt(topic):
    """Send an MQTT message."""
    try:
        client = mqtt.Client("P1")
        client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
        client.connect(MQTT_BROKER)
        client.publish(str(topic), "1")
        client.disconnect()
        logger.info(f"MQTT message sent to topic: {topic}")
        
        # Also publish to Home Assistant state topic
        ha_topic = f"homeassistant/binary_sensor/{topic.replace(' ', '_')}/state"
        client = mqtt.Client("P2")
        client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
        client.connect(MQTT_BROKER)
        client.publish(ha_topic, "ON")  # Set to ON when alarm is triggered
        client.disconnect()
    except Exception as e:
        logger.error(f"Error sending MQTT message: {e}")

def handle_alarm_timeout(sensor_id):
    """Handle the timeout of an alarm by removing it from tracking."""
    global alarm_timers, sent_alarms
    
    try:
        # Remove the alarm timer reference
        if sensor_id in alarm_timers:
            del alarm_timers[sensor_id]
        
        # Remove from sent alarms if it was there
        if sensor_id in sent_alarms:
            sent_alarms.remove(sensor_id)
        
        logger.info(f"Alarm for sensor {sensor_id} has timed out and been cleared")
        
        # Send MQTT message to reset state
        topic = "homeassistant/binary_sensor/"
        if sensor_id in list_of_cold_room_sensors:
            topic += "cold_room/state"
        elif sensor_id in list_of_normal_room_sensors:
            topic += "normal_room/state"
        else:
            topic += f"sensor_{sensor_id}/state"
            
        try:
            client = mqtt.Client("P3")
            client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
            client.connect(MQTT_BROKER)
            client.publish(topic, "OFF")  # Reset to OFF when alarm timeout
            client.disconnect()
        except Exception as e:
            logger.error(f"Error sending MQTT reset message: {e}")
            
    except Exception as e:
        logger.error(f"Error handling alarm timeout for sensor {sensor_id}: {e}")

def process_alarm(alarm_type, sensor_id, gateway_id, value, timestamp):
    """Process an incoming alarm."""
    global alarm_queue, alarm_timers, sent_alarms
    
    try:
        # Check if we already have an active alarm for this sensor
        if sensor_id in alarm_timers:
            logger.info(f"Alarm for sensor {sensor_id} already exists, updating values")
            # We could update values here if needed
            return
            
        # Create new alarm object
        new_alarm = Alarm(alarm_type, sensor_id, gateway_id, value, timestamp)
        
        # Add to queue for SMS sending
        alarm_queue.put(new_alarm)
        logger.info(f"New alarm queued for sensor {sensor_id}")
        
        # Set timeout for alarm based on sensor type
        timeout = None
        if str(sensor_id) in list_of_cold_room_sensors:
            timeout = COLD_ROOM_TIMEOUT * 60  # Convert to seconds
            logger.info(f"Setting cold room timeout of {timeout} seconds for sensor {sensor_id}")
            send_mqtt("cold_room")
        elif str(sensor_id) in list_of_normal_room_sensors:
            timeout = NORMAL_ROOM_TIMEOUT * 60  # Convert to seconds
            logger.info(f"Setting normal room timeout of {timeout} seconds for sensor {sensor_id}")
            send_mqtt("normal_room")
        else:
            # Default timeout if not in either list
            timeout = 20 * 60  # 20 minutes in seconds
            logger.warning(f"Sensor {sensor_id} not found in either cold or normal room lists, using default timeout")
            send_mqtt(f"sensor_{sensor_id}")
            
        # Create and start timer for this alarm
        timer = threading.Timer(timeout, handle_alarm_timeout, args=[sensor_id])
        timer.daemon = True
        timer.start()
        alarm_timers[sensor_id] = timer
            
    except Exception as e:
        logger.error(f"Error processing alarm: {e}")

def parse_alarm_data(data):
    """Parse the alarm data received from the TCP connection."""
    try:
        data = data.replace("\n", "").replace("b'", "").replace("\n\n'", "")
        sections = data.split("}")
        
        type_of_alarm = sections[0].split(',')[0].split(":")[1].replace("\"", "")
        sensor_id = sections[4].split(',')[-1].split(":")[1].replace("\"", "")
        gateway_id = sections[4].split(',')[-2].split(":")[2].replace("\"", "")
        value = sections[5].split(',')[4].replace("]]", "")
        timestamp = sections[5].split(',')[3].replace("]]", "").replace("[[", "").split("\"")[3]
        
        logger.info(f"Parsed alarm: Type={type_of_alarm}, Sensor={sensor_id}, Value={value}")
        process_alarm(type_of_alarm, sensor_id, gateway_id, value, timestamp)
    except Exception as e:
        logger.error(f"Error parsing alarm data: {e}, Raw data: {data}")

class AlarmHandler(asyncore.dispatcher_with_send):
    """Handler for incoming TCP connections with alarm data."""
    
    def handle_read(self):
        try:
            data = self.recv(8192)
            if data:
                parse_alarm_data(str(data))
        except Exception as e:
            logger.error(f"Error in handle_read: {e}")

class AlarmServer(asyncore.dispatcher):
    """TCP server that listens for incoming alarm data."""
    
    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.create_socket()
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(5)
        logger.info(f"Alarm server started on {host}:{port}")
    
    def handle_accepted(self, sock, addr):
        logger.info(f'Incoming connection from {addr}')
        AlarmHandler(sock)

def run_sms_sender():
    """Run the SMS sender in a loop."""
    global alarm_queue, sent_alarms
    
    if not phone_numbers:
        logger.warning("No phone numbers loaded for SMS sending")
    
    while True:
        try:
            # Check if we have any alarms to process
            if not alarm_queue.empty():
                # Get the alarm from the queue but don't remove it yet
                alarm = alarm_queue.queue[0]
                
                # Skip if we've already sent SMS for this sensor
                if alarm.sensor_id in sent_alarms:
                    # Remove from queue and continue
                    alarm_queue.get()
                    alarm_queue.task_done()
                    continue
                    
                logger.info(f"Processing alarm for sensor {alarm.sensor_id}")
                
                # Send SMS to each number
                success_count = 0
                for number in phone_numbers:
                    if send_sms(str(alarm), number):
                        success_count += 1
                    time.sleep(SMS_DELAY)  # Delay between messages
                
                # If at least one SMS was sent successfully, mark as sent
                if success_count > 0:
                    logger.info(f"Successfully sent {success_count}/{len(phone_numbers)} SMS notifications for sensor {alarm.sensor_id}")
                    sent_alarms.add(alarm.sensor_id)
                else:
                    logger.warning(f"Failed to send any SMS notifications for sensor {alarm.sensor_id}")
                
                # Remove from queue
                alarm_queue.get()
                alarm_queue.task_done()
            
            time.sleep(5)  # Check for new alarms every 5 seconds
        except Exception as e:
            logger.error(f"Error in SMS sender loop: {e}")
            time.sleep(60)  # Wait longer after an error

def setup_hass_mqtt_discovery():
    """Set up Home Assistant MQTT discovery for the sensors."""
    try:
        client = mqtt.Client("P4")
        client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
        client.connect(MQTT_BROKER)
        
        # Set up cold room binary sensor
        cold_config = {
            "name": "Cold Room Alarm",
            "unique_id": "cold_room_alarm",
            "state_topic": "homeassistant/binary_sensor/cold_room/state",
            "device_class": "problem",
            "payload_on": "ON",
            "payload_off": "OFF"
        }
        client.publish("homeassistant/binary_sensor/cold_room/config", str(cold_config).replace("'", "\""))
        
        # Set up normal room binary sensor
        normal_config = {
            "name": "Normal Room Alarm",
            "unique_id": "normal_room_alarm",
            "state_topic": "homeassistant/binary_sensor/normal_room/state",
            "device_class": "problem",
            "payload_on": "ON",
            "payload_off": "OFF"
        }
        client.publish("homeassistant/binary_sensor/normal_room/config", str(normal_config).replace("'", "\""))
        
        client.disconnect()
        logger.info("MQTT discovery configurations sent to Home Assistant")
    except Exception as e:
        logger.error(f"Error setting up MQTT discovery: {e}")

def main():
    """Main function to start all components."""
    try:
        # Load configurations        
        logger.info(f"Loaded {len(list_of_cold_room_sensors)} cold room sensors")
        logger.info(f"Loaded {len(list_of_normal_room_sensors)} normal room sensors")
        
        # Set up Home Assistant integration
        setup_hass_mqtt_discovery()
        
        # Start the SMS sender in a separate thread
        sms_thread = threading.Thread(target=run_sms_sender, daemon=True)
        sms_thread.start()
        logger.info("SMS sender thread started")
        
        # Start the TCP server to listen for alarms
        server = AlarmServer(TCP_SERVER_HOST, TCP_SERVER_PORT)
        logger.info("TCP server started")
        
        # Start the asyncore loop in the main thread
        logger.info("Starting main event loop")
        asyncore.loop()
    except Exception as e:
        logger.error(f"Error in main function: {e}")

if __name__ == "__main__":
    main()
