#!/usr/bin/env python3
import time
import json
import logging
import serial
import paho.mqtt.client as mqtt
import ssl
import csv
from threading import Event
from datetime import datetime
import os
import sys

class GMC300EPlus:
    """Direct serial communication with GMC-300E Plus using GQ-RFC1201 protocol"""
    
    def __init__(self, port='/dev/ttyUSB0', timeout=3):
        self.ser = serial.Serial(
            port=port,
            baudrate=115200,  # GMC-300E Plus V4.xx firmware
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False
        )
        time.sleep(0.5)  # Allow port to settle
    
    def send_command(self, command, expected_length=None):
        """Send command and return response with error handling"""
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            
            self.ser.write(command.encode('ascii'))
            self.ser.flush()
            time.sleep(0.5)  # Allow device to process
            
            if expected_length:
                response = self.ser.read(expected_length)
            else:
                response = self.ser.read_all()
                
            return response
        except Exception as e:
            raise ConnectionError(f"Communication failed: {e}")
    
    def get_version(self):
        """Get device version string"""
        response = self.send_command('<GETVER>>', 14)
        if len(response) != 14:
            raise ValueError(f"Invalid version response length: {len(response)}")
        return response.decode('ascii', errors='ignore').strip()
    
    def get_cpm(self):
        """Get current CPM reading"""
        response = self.send_command('<GETCPM>>', 2)
        if len(response) != 2:
            raise ValueError(f"Invalid CPM response length: {len(response)}")
        return (response[0] << 8) | response[1]
    
    def get_battery_voltage(self):
        """Get battery voltage in volts"""
        response = self.send_command('<GETVOLT>>', 1)
        if len(response) != 1:
            raise ValueError(f"Invalid voltage response length: {len(response)}")
        return response[0] / 10.0
    
    def set_datetime(self, dt=None):
        """Set device date and time (defaults to current system time)"""
        if dt is None:
            dt = datetime.now()
        
        # Convert to 2-digit year (assumes 21st century)
        yy = dt.year % 100
        
        # Format as hexadecimal string
        datetime_hex = f'{yy:02X}{dt.month:02X}{dt.day:02X}{dt.hour:02X}{dt.minute:02X}{dt.second:02X}'
        command = f'<SETDATETIME[{datetime_hex}]>>'
        
        response = self.send_command(command, 1)
        if len(response) != 1 or response[0] != 0xAA:
            raise ValueError("Failed to set date/time - device returned error")
        
        return True
    
    def get_datetime(self):
        """Get device date and time"""
        response = self.send_command('<GETDATETIME>>', 7)
        if len(response) != 7 or response[6] != 0xAA:
            raise ValueError("Invalid datetime response")
        
        # Convert 2-digit year to 4-digit (assumes 21st century for years < 50)
        year = 2000 + response[0] if response[0] < 50 else 1900 + response[0]
        
        return {
            'year': year,
            'month': response[1],
            'day': response[2], 
            'hour': response[3],
            'minute': response[4],
            'second': response[5],
            'datetime': datetime(year, response[1], response[2], 
                               response[3], response[4], response[5])
        }
    
    def disconnect(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            self.ser.close()

class DataLogger:
    """CSV data logging for backup and historical analysis"""
    
    def __init__(self, log_file, max_file_size_mb=100):
        self.log_file = log_file
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.fieldnames = [
            'timestamp', 'datetime', 'cpm', 'uSv_h', 
            'battery_voltage', 'battery_percent'
        ]
        self._ensure_log_file()
    
    def _ensure_log_file(self):
        """Create log file with headers if it doesn't exist"""
        if not os.path.exists(self.log_file):
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()
    
    def log_reading(self, data):
        """Log a reading to CSV file"""
        try:
            # Rotate log file if too large
            if os.path.getsize(self.log_file) > self.max_file_size:
                self._rotate_log_file()
            
            with open(self.log_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writerow(data)
                
        except Exception as e:
            logging.error(f"Failed to log data: {e}")
    
    def _rotate_log_file(self):
        """Rotate log file when it gets too large"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            archived_file = f"{self.log_file}.{timestamp}"
            os.rename(self.log_file, archived_file)
            self._ensure_log_file()
            logging.info(f"Log file rotated to {archived_file}")
        except Exception as e:
            logging.error(f"Failed to rotate log file: {e}")
    
    def export_data(self, start_date=None, end_date=None, output_file=None):
        """Export historical data within date range"""
        if not output_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"gmc_export_{timestamp}.csv"
        
        try:
            with open(self.log_file, 'r') as infile, open(output_file, 'w', newline='') as outfile:
                reader = csv.DictReader(infile)
                writer = csv.DictWriter(outfile, fieldnames=self.fieldnames)
                writer.writeheader()
                
                for row in reader:
                    if start_date or end_date:
                        try:
                            row_time = datetime.fromisoformat(row['datetime'])
                            if start_date and row_time < start_date:
                                continue
                            if end_date and row_time > end_date:
                                continue
                        except (ValueError, KeyError):
                            continue
                    
                    writer.writerow(row)
            
            logging.info(f"Data exported to {output_file}")
            return output_file
            
        except Exception as e:
            logging.error(f"Failed to export data: {e}")
            return None

class AlertManager:
    """Handle configurable alerting"""
    
    def __init__(self, config):
        self.config = config
        self.alert_states = {}
        self.last_alerts = {}
    
    def check_alerts(self, data):
        """Check all configured alert conditions"""
        alerts = []
        
        # High radiation alert
        if self.config.get('high_radiation_threshold_usvh'):
            threshold = self.config['high_radiation_threshold_usvh']
            duration = self.config.get('high_radiation_duration_minutes', 0) * 60
            
            if data['uSv_h'] > threshold:
                alert_key = 'high_radiation'
                if self._should_trigger_alert(alert_key, duration):
                    alerts.append({
                        'type': 'high_radiation',
                        'message': f"High radiation detected: {data['uSv_h']:.3f} µSv/h (threshold: {threshold})",
                        'level': 'warning',
                        'data': data
                    })
            else:
                self._clear_alert_state('high_radiation')
        
        # Battery alerts
        if self.config.get('enable_battery_alerts', True):
            if data['battery_voltage'] < self.config.get('critical_battery_threshold_volts', 5.5):
                alerts.append({
                    'type': 'critical_battery',
                    'message': f"Critical battery level: {data['battery_voltage']:.1f}V",
                    'level': 'critical',
                    'data': data
                })
            elif data['battery_voltage'] < self.config.get('low_battery_threshold_volts', 6.0):
                alert_key = 'low_battery'
                if self._should_trigger_alert(alert_key, 300):  # 5 min delay
                    alerts.append({
                        'type': 'low_battery',
                        'message': f"Low battery: {data['battery_voltage']:.1f}V",
                        'level': 'warning',
                        'data': data
                    })
        
        return alerts
    
    def _should_trigger_alert(self, alert_key, min_duration=0):
        """Check if alert should trigger based on duration"""
        current_time = time.time()
        
        if alert_key not in self.alert_states:
            self.alert_states[alert_key] = current_time
        
        # Check if enough time has passed
        if current_time - self.alert_states[alert_key] >= min_duration:
            # Check if we haven't sent this alert recently (avoid spam)
            if alert_key not in self.last_alerts or current_time - self.last_alerts[alert_key] > 3600:
                self.last_alerts[alert_key] = current_time
                return True
        
        return False
    
    def _clear_alert_state(self, alert_key):
        """Clear alert state when condition is no longer met"""
        if alert_key in self.alert_states:
            del self.alert_states[alert_key]

class GMCMonitor:
    def __init__(self, config_file='gmc_config.json'):
        self.config = self.load_config(config_file)
        self.mqtt_client = mqtt.Client()
        self.device = None
        self.running = Event()
        self.data_logger = None
        self.alert_manager = AlertManager(self.config.get('alerts', {}))
        self.setup_logging()
        self.setup_data_logging()
        self.connection_start_time = time.time()
    
    def load_config(self, config_file):
        """Load configuration from JSON file"""
        default_config = {
            "device": {
                "port": "/dev/ttyUSB0",
                "timeout": 3,
                "sync_datetime_on_start": True,
                "check_time_drift": True,
                "max_time_drift_seconds": 300,
                "cpm_to_usvh_factor": 0.0057
            },
            "mqtt": {
                "broker": "localhost",
                "port": 1883,
                "username": None,
                "password": None,
                "topic_prefix": "homeassistant/sensor/gmc300e",
                "discovery_prefix": "homeassistant",
                "use_ssl": False,
                "ca_cert": None,
                "cert_file": None,
                "key_file": None,
                "insecure": False,
                "availability_topic": "homeassistant/sensor/gmc300e/availability"
            },
            "monitoring": {
                "update_interval_seconds": 60,
                "low_battery_threshold_volts": 6.0,
                "critical_battery_threshold_volts": 5.5,
                "battery_full_voltage": 8.4,
                "battery_empty_voltage": 6.0
            },
            "logging": {
                "level": "INFO",
                "file": None
            },
            "data_logging": {
                "enabled": True,
                "csv_file": "gmc_data.csv",
                "max_file_size_mb": 100,
                "enable_export": True
            },
            "alerts": {
                "high_radiation_threshold_usvh": 0.5,
                "high_radiation_duration_minutes": 2,
                "enable_battery_alerts": True,
                "low_battery_threshold_volts": 6.0,
                "critical_battery_threshold_volts": 5.5
            },
            "homeassistant": {
                "device_name": "GMC-300E Plus",
                "device_model": "GMC-300E+",
                "device_manufacturer": "GQ Electronics",
                "device_identifier": "gmc300e_plus"
            }
        }
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    user_config = json.load(f)
                # Merge user config with defaults
                config = self._merge_config(default_config, user_config)
                print(f"Loaded configuration from {config_file}")
            except Exception as e:
                print(f"Error loading config file {config_file}: {e}")
                print("Using default configuration")
                config = default_config
        else:
            # Create default config file
            try:
                with open(config_file, 'w') as f:
                    json.dump(default_config, f, indent=2)
                print(f"Created default configuration file: {config_file}")
            except Exception as e:
                print(f"Could not create config file: {e}")
            config = default_config
        
        return config
    
    def _merge_config(self, default, user):
        """Recursively merge user config with defaults"""
        result = default.copy()
        for key, value in user.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result
    
    def setup_logging(self):
        log_config = self.config['logging']
        log_level = getattr(logging, log_config['level'].upper(), logging.INFO)
        
        logging_kwargs = {
            'level': log_level,
            'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        }
        
        if log_config['file']:
            logging_kwargs['filename'] = log_config['file']
        
        logging.basicConfig(**logging_kwargs)
        self.logger = logging.getLogger(__name__)
    
    def setup_data_logging(self):
        """Initialize CSV data logging if enabled"""
        if self.config['data_logging']['enabled']:
            self.data_logger = DataLogger(
                self.config['data_logging']['csv_file'],
                self.config['data_logging']['max_file_size_mb']
            )
    
    def setup_mqtt_ssl(self):
        """Configure MQTT SSL/TLS if enabled"""
        mqtt_config = self.config['mqtt']
        
        if mqtt_config.get('use_ssl', False):
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            
            if mqtt_config.get('ca_cert'):
                context.load_verify_locations(mqtt_config['ca_cert'])
            
            if mqtt_config.get('cert_file') and mqtt_config.get('key_file'):
                context.load_cert_chain(mqtt_config['cert_file'], mqtt_config['key_file'])
            
            if mqtt_config.get('insecure', False):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            
            self.mqtt_client.tls_set_context(context)
            self.logger.info("MQTT SSL/TLS configured")
    
    def connect_device(self):
        """Connect to GMC-300E Plus and optionally sync time"""
        device_config = self.config['device']
        
        try:
            self.device = GMC300EPlus(
                port=device_config['port'],
                timeout=device_config['timeout']
            )
            version = self.device.get_version()
            self.logger.info(f"Connected to: {version}")
            self.connection_start_time = time.time()
            
            # Sync device time with system time if configured
            if device_config['sync_datetime_on_start']:
                try:
                    self.device.set_datetime()
                    device_time = self.device.get_datetime()
                    self.logger.info(f"Device time synced: {device_time['datetime']}")
                except Exception as e:
                    self.logger.warning(f"Time sync failed: {e}")
            
            # Check battery voltage
            voltage = self.device.get_battery_voltage()
            self.logger.info(f"Battery voltage: {voltage:.1f}V")
            
            battery_config = self.config['monitoring']
            if voltage < battery_config['low_battery_threshold_volts']:
                self.logger.warning("Low battery detected!")
            elif voltage < battery_config['critical_battery_threshold_volts']:
                self.logger.error("Critical battery level!")
            
            # Publish device online status
            self.publish_availability(True)
            self.publish_discovery()
            return True
            
        except Exception as e:
            self.logger.error(f"Device connection failed: {e}")
            if self.device:
                self.device.disconnect()
                self.device = None
            self.publish_availability(False)
        return False
    
    def publish_availability(self, available):
        """Publish device availability status"""
        availability_topic = self.config['mqtt']['availability_topic']
        payload = "online" if available else "offline"
        self.mqtt_client.publish(availability_topic, payload, retain=True)
    
    def publish_discovery(self):
        """Publish Home Assistant MQTT discovery configs"""
        ha_config = self.config['homeassistant']
        mqtt_config = self.config['mqtt']
        
        device_info = {
            "identifiers": [ha_config['device_identifier']],
            "name": ha_config['device_name'],
            "model": ha_config['device_model'], 
            "manufacturer": ha_config['device_manufacturer']
        }
        
        state_topic = f"{mqtt_config['topic_prefix']}/state"
        availability_topic = mqtt_config['availability_topic']
        
        # Base sensor configuration
        base_sensor_config = {
            "device": device_info,
            "availability_topic": availability_topic,
            "state_topic": state_topic
        }
        
        # Define all sensors
        sensors = [
            {
                "name": "Radiation CPM",
                "device_class": "radiation",
                "value_template": "{{ value_json.cpm }}",
                "unit_of_measurement": "CPM",
                "unique_id": f"{ha_config['device_identifier']}_cpm"
            },
            {
                "name": "Radiation µSv/h", 
                "device_class": "radiation",
                "value_template": "{{ value_json.uSv_h }}",
                "unit_of_measurement": "µSv/h",
                "unique_id": f"{ha_config['device_identifier']}_usvh"
            },
            {
                "name": "GMC Battery Voltage",
                "device_class": "voltage",
                "value_template": "{{ value_json.battery_voltage }}",
                "unit_of_measurement": "V",
                "unique_id": f"{ha_config['device_identifier']}_voltage"
            },
            {
                "name": "GMC Battery Level",
                "device_class": "battery", 
                "value_template": "{{ value_json.battery_percent }}",
                "unit_of_measurement": "%",
                "unique_id": f"{ha_config['device_identifier']}_battery"
            },
            {
                "name": "GMC Connection Status",
                "value_template": "{{ value_json.connection_status }}",
                "unique_id": f"{ha_config['device_identifier']}_connection"
            }
        ]
        
        # Publish discovery configs
        discovery_prefix = mqtt_config['discovery_prefix']
        for sensor in sensors:
            config = {**base_sensor_config, **sensor}
            topic = f"{discovery_prefix}/sensor/{sensor['unique_id']}/config"
            self.mqtt_client.publish(topic, json.dumps(config), retain=True)
            
        self.logger.info("Published MQTT discovery configs")
    
    def calculate_battery_percentage(self, voltage):
        """Convert voltage to battery percentage based on config"""
        battery_config = self.config['monitoring']
        full_voltage = battery_config['battery_full_voltage']
        empty_voltage = battery_config['battery_empty_voltage']
        
        if voltage >= full_voltage:
            return 100
        elif voltage <= empty_voltage:
            return 0
        else:
            # Linear interpolation between empty and full
            percentage = ((voltage - empty_voltage) / (full_voltage - empty_voltage)) * 100
            return max(0, min(100, int(percentage)))
    
    def read_and_publish(self):
        """Read all sensor data and publish to MQTT"""
        try:
            # Get radiation reading
            cpm = self.device.get_cpm()
            
            # Get battery status
            voltage = self.device.get_battery_voltage()
            battery_percent = self.calculate_battery_percentage(voltage)
            
            # Calculate dose rate using configured conversion factor
            device_config = self.config['device']
            usvh = cpm * device_config['cpm_to_usvh_factor']
            
            # Check device time drift if configured
            if device_config['check_time_drift']:
                try:
                    device_time = self.device.get_datetime()
                    time_drift = abs((datetime.now() - device_time['datetime']).total_seconds())
                    max_drift = device_config['max_time_drift_seconds']
                    
                    if time_drift > max_drift:
                        self.logger.warning(f"Device time drift: {time_drift:.0f} seconds")
                        if device_config['sync_datetime_on_start']:  # Only auto-sync if enabled
                            self.device.set_datetime()
                            self.logger.info("Device time re-synchronized")
                except Exception as e:
                    self.logger.debug(f"Time check failed: {e}")
            
            # Prepare main payload
            current_time = datetime.now()
            payload = {
                "cpm": cpm,
                "uSv_h": round(usvh, 3),
                "battery_voltage": round(voltage, 1),
                "battery_percent": battery_percent,
                "connection_status": "Connected",
                "timestamp": time.time(),
                "last_updated": current_time.isoformat()
            }
            
            # Log to CSV if enabled
            if self.data_logger:
                csv_data = {
                    'timestamp': payload['timestamp'],
                    'datetime': payload['last_updated'],
                    'cpm': cpm,
                    'uSv_h': payload['uSv_h'],
                    'battery_voltage': payload['battery_voltage'],
                    'battery_percent': battery_percent
                }
                self.data_logger.log_reading(csv_data)
            
            # Check for alerts
            alerts = self.alert_manager.check_alerts(payload)
            for alert in alerts:
                self.logger.log(
                    logging.WARNING if alert['level'] == 'warning' else logging.ERROR,
                    alert['message']
                )
            
            # Publish to MQTT
            mqtt_config = self.config['mqtt']
            state_topic = f"{mqtt_config['topic_prefix']}/state"
            self.mqtt_client.publish(state_topic, json.dumps(payload), retain=True)
            
            self.logger.info(
                f"Published: {cpm} CPM ({usvh:.3f} µSv/h), "
                f"Battery: {voltage:.1f}V ({battery_percent}%)"
            )
            
        except Exception as e:
            self.logger.error(f"Reading failed: {e}")
            self.reconnect_device()
    
    def reconnect_device(self):
        """Reconnect to device after failure"""
        self.logger.info("Attempting to reconnect...")
        self.publish_availability(False)
        
        if self.device:
            try:
                self.device.disconnect()
            except:
                pass
            self.device = None
        
        time.sleep(5)
        self.connect_device()
    
    def on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.logger.info("Connected to MQTT broker")
            # Set LWT (Last Will and Testament) for availability
            availability_topic = self.config['mqtt']['availability_topic']
            client.will_set(availability_topic, "offline", retain=True)
        else:
            self.logger.error(f"MQTT connection failed with code {rc}")
    
    def on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        self.logger.warning(f"Disconnected from MQTT broker (code: {rc})")
    
    def run(self):
        """Main monitoring loop"""
        mqtt_config = self.config['mqtt']
        monitoring_config = self.config['monitoring']
        
        # Setup MQTT callbacks
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
        # Configure MQTT SSL if enabled
        self.setup_mqtt_ssl()
        
        # Connect to MQTT broker
        try:
            if mqtt_config.get('username') and mqtt_config.get('password'):
                self.mqtt_client.username_pw_set(
                    mqtt_config['username'], 
                    mqtt_config['password']
                )
            
            # Set availability LWT before connecting
            availability_topic = mqtt_config['availability_topic']
            self.mqtt_client.will_set(availability_topic, "offline", retain=True)
            
            self.mqtt_client.connect(
                mqtt_config['broker'], 
                mqtt_config['port'], 
                60
            )
            self.mqtt_client.loop_start()
            self.logger.info(f"Connected to MQTT broker at {mqtt_config['broker']}:{mqtt_config['port']}")
        except Exception as e:
            self.logger.error(f"MQTT connection failed: {e}")
            return
        
        # Connect to device
        if not self.connect_device():
            self.logger.error("Initial device connection failed")
            return
        
        # Main monitoring loop
        self.running.set()
        update_interval = monitoring_config['update_interval_seconds']
        
        try:
            while self.running.is_set():
                try:
                    self.read_and_publish()
                    time.sleep(update_interval)
                except KeyboardInterrupt:
                    self.logger.info("Shutdown requested")
                    self.running.clear()
                except Exception as e:
                    self.logger.error(f"Unexpected error: {e}")
                    time.sleep(10)  # Wait before retry
        finally:
            # Cleanup
            self.publish_availability(False)
            if self.device:
                self.device.disconnect()
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.logger.info("Monitor stopped")

if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'gmc_config.json'
    
    monitor = GMCMonitor(config_file)
    monitor.run()