# countdown.py
import threading
import time
from datetime import datetime, timedelta
import logging

# Import the new API manager
from vt_api_manager import QLabAPIClient, VmixAPIClient, OSCServerManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class Countdown(threading.Thread):
    """
    Represents a single countdown instance, running in its own thread.
    Notifies AudioManager for verbalizations and emits status updates.
    """

    # Define common time intervals in seconds
    VERBAL_INTERVALS = {
        '1h': 3600, '45m': 2700, '30m': 1800, '20m': 1200, '15m': 900,
        '10m': 600, '5m': 300, '3m': 180, '2m': 120, '90s': 90,
        '60s': 60, '45s': 45, '30s': 30, '20s': 20, '15s': 15,
        '10s': 10, '5s': 5, '4s': 4, '3s': 3, '2s': 2, '1s': 1
    }
    
    POLLING_INTERVAL_SECONDS = 0.5 # 500ms

    def __init__(self, countdown_id, countdown_type, name, target_config, # Renamed for flexibility
                 verbalization_settings, audio_manager, socketio, osc_server_manager: OSCServerManager = None):
        super().__init__()
        self.id = countdown_id
        self.type = countdown_type # 'item' or 'vt'
        self.name = name
        self.verbalization_settings = verbalization_settings
        self.audio_manager = audio_manager
        self.socketio = socketio
        self._stop_event = threading.Event()
        self.last_spoken_interval = {} # To track which intervals have been spoken

        self.target_datetime = None # For 'item' type
        self.last_known_remaining_seconds = None # For 'vt' type
        self.vt_api_clients = {} # Stores QLab and vMix client instances
        self.vt_api_config = target_config # Stores API details for 'vt' type
        self.osc_server_manager = osc_server_manager # Reference to the global OSC server

        if self.type == 'item':
            # target_config is a datetime object
            self.target_datetime = target_config
            logging.info(f"Countdown '{self.name}' (ID: {self.id}) initialized for item at {self.target_datetime}")
        elif self.type == 'vt':
            # target_config is a dictionary with API details
            if self.vt_api_config.get('qlab_ip') and self.vt_api_config.get('qlab_send_port') and self.osc_server_manager:
                # Pass the OSC server manager to QLabAPIClient so it can read state
                self.vt_api_clients['qlab'] = QLabAPIClient(
                    self.vt_api_config['qlab_ip'],
                    self.vt_api_config['qlab_send_port'],
                    self.osc_server_manager
                )
            if self.vt_api_config.get('vmix_ip') and self.vt_api_config.get('vmix_port'):
                self.vt_api_clients['vmix'] = VmixAPIClient(self.vt_api_config['vmix_ip'], self.vt_api_config['vmix_port'])
            
            logging.info(f"Countdown '{self.name}' (ID: {self.id}) initialized for VT mode with API polling.")
        else:
            raise ValueError(f"Unknown countdown type: {countdown_type}")

        # Initialize last_spoken_interval for all active intervals
        for interval_key in verbalization_settings['active_intervals']:
            self.last_spoken_interval[interval_key] = False # True once spoken
        # Also initialize for dynamic 10s/5s countdown
        if verbalization_settings.get('countdown_from_10_5_secs'):
            for i in range(1, 11): # Covers 1 to 10 seconds
                self.last_spoken_interval[f'{i}s_countdown'] = False
            if verbalization_settings.get('verbalize_10s_on_5s_countdown'):
                self.last_spoken_interval['10s_on_5s_countdown'] = False


    def run(self):
        """Main loop for the countdown thread."""
        logging.info(f"Countdown '{self.name}' (ID: {self.id}) thread started.")
        while not self._stop_event.is_set():
            remaining_seconds = None

            if self.type == 'item':
                remaining_seconds = (self.target_datetime - datetime.now()).total_seconds()
            elif self.type == 'vt':
                remaining_seconds, source_name = self._get_vt_remaining_time_from_api()
                if remaining_seconds is not None:
                    self.last_known_remaining_seconds = remaining_seconds
                else:
                    # If API fails or no video found, use last known or default to zero
                    remaining_seconds = self.last_known_remaining_seconds if self.last_known_remaining_seconds is not None else 0
                    if remaining_seconds <= 0: # Ensure it doesn't show negative if API drops
                        remaining_seconds = 0
            
            if remaining_seconds is None or remaining_seconds <= 0:
                # For 'item' countdowns, this means it's finished.
                # For 'vt' countdowns, this might mean no video is active or API issue.
                if self.type == 'item':
                    self._handle_countdown_finished()
                    break # Stop thread for item countdown if finished
                else: # VT countdown, keep polling if API is enabled but no video found
                    self._send_status_update(remaining_seconds) # Send 0 or last known state
                    time.sleep(self.POLLING_INTERVAL_SECONDS)
                    continue


            self._check_and_speak(remaining_seconds)
            self._send_status_update(remaining_seconds)

            time.sleep(self.POLLING_INTERVAL_SECONDS) # Poll every 500ms

        logging.info(f"Countdown '{self.name}' (ID: {self.id}) thread finished.")
        self._send_status_update(0) # Final update for UI to show as finished/removed

    def _get_vt_remaining_time_from_api(self) -> tuple[float | None, str | None]:
        """
        Queries vMix and/or QLab APIs based on configuration and returns remaining time.
        Prioritization: vMix Program -> vMix Fallback 1 -> vMix Fallback 2 -> QLab.
        """
        # 1. Check vMix Program Output (if vMix client is available)
        if 'vmix' in self.vt_api_clients:
            vmix_client: VmixAPIClient = self.vt_api_clients['vmix']
            xml_root = vmix_client.get_status_xml()
            if xml_root:
                remaining_s, title = vmix_client.get_program_video_remaining_time(xml_root)
                if remaining_s is not None:
                    logging.debug(f"vMix Program Output: {title}, Remaining: {remaining_s:.2f}s")
                    return remaining_s, title

        # 2. Check vMix Fallback Inputs (if vMix client is available and fallbacks are configured)
        if 'vmix' in self.vt_api_clients:
            vmix_client: VmixAPIClient = self.vt_api_clients['vmix']
            xml_root = vmix_client.get_status_xml() # Re-fetch in case of timing, or pass xml_root
            if xml_root:
                for fallback_key in ['vmix_fallback1_input', 'vmix_fallback2_input']:
                    input_id = self.vt_api_config.get(fallback_key)
                    if input_id:
                        remaining_s, title = vmix_client.get_video_input_status(xml_root, input_id)
                        if remaining_s is not None:
                            logging.debug(f"vMix Fallback '{input_id}': {title}, Remaining: {remaining_s:.2f}s")
                            return remaining_s, title

        # 3. Check QLab (if QLab client is available)
        if 'qlab' in self.vt_api_clients:
            qlab_client: QLabAPIClient = self.vt_api_clients['qlab']
            # This method now uses the OSC server to get the latest QLab state
            remaining_s, title = qlab_client.get_active_video_remaining_time()
            if remaining_s is not None:
                logging.debug(f"QLab Active Video: {title}, Remaining: {remaining_s:.2f}s")
                return remaining_s, title
            else:
                logging.debug("QLab: No active video cue found or data not yet received.")


        return None, None # No active video found or API clients not configured/reachable

    def _check_and_speak(self, remaining_seconds):
        """Checks if any verbalization intervals should be triggered."""
        format_string = self.verbalization_settings['format_string']
        name_placeholder = self.name if self.name else "" # Use name or empty string if not set

        # Handle 10-second or 5-second specific countdown (if enabled)
        countdown_mode = self.verbalization_settings.get('countdown_from_10_5_secs')
        if countdown_mode:
            start_count = 10 if countdown_mode == '10s' else 5
            
            # Special case: verbalize 10s if 5s countdown selected AND verbalize_10s_on_5s_countdown is true
            if countdown_mode == '5s' and self.verbalization_settings.get('verbalize_10s_on_5s_countdown') and \
               remaining_seconds > 5 and remaining_seconds <= 10:
                if not self.last_spoken_interval.get('10s_on_5s_countdown'):
                    message = format_string.format(name=name_placeholder, time_value=10, time_unit='seconds')
                    self.audio_manager.speak(message, remaining_seconds)
                    self.last_spoken_interval['10s_on_5s_countdown'] = True
            
            # Main 10s/5s countdown loop
            for i in range(start_count, 0, -1):
                # Check if we are at integer second mark (within a small tolerance)
                # and if this specific second hasn't been spoken yet.
                if i == int(round(remaining_seconds, 0)) and \
                   remaining_seconds >= i - 0.1 and remaining_seconds <= i + 0.1: # Catch exact second
                    if not self.last_spoken_interval.get(f'{i}s_countdown'):
                        message = format_string.format(name=name_placeholder, time_value=i, time_unit='seconds')
                        self.audio_manager.speak(message, remaining_seconds)
                        self.last_spoken_interval[f'{i}s_countdown'] = True
            
            if remaining_seconds <= start_count:
                return # Don't check other intervals if specific countdown is active and relevant

        # Handle standard intervals
        for interval_key, interval_seconds in self.VERBAL_INTERVALS.items():
            # Skip single-second intervals if part of a specific 10s/5s countdown mode (already handled above)
            if 's' in interval_key and int(interval_key.replace('s', '')) < 11 and countdown_mode:
                continue

            # Only verbalize if the interval is active in settings and hasn't been spoken yet
            if interval_key in self.verbalization_settings['active_intervals'] and not self.last_spoken_interval.get(interval_key):
                # Use a small buffer to ensure we catch the exact second.
                if remaining_seconds <= interval_seconds + (self.POLLING_INTERVAL_SECONDS / 2): # e.g., for 60s, trigger between 60.25 and 60.0
                    message = ""
                    time_value = interval_seconds
                    time_unit = "seconds" # Default

                    if interval_seconds == 3600:
                        time_value = 1
                        time_unit = "hour"
                    elif interval_seconds >= 60:
                        time_value = interval_seconds // 60
                        time_unit = "minutes"
                    elif interval_seconds == 90:
                        time_value = 90
                        time_unit = "seconds" # Special case for 90s
                    elif interval_seconds == 1:
                        time_unit = "second" # Singular

                    message = format_string.format(name=name_placeholder, time_value=time_value, time_unit=time_unit)
                    self.audio_manager.speak(message, remaining_seconds)
                    self.last_spoken_interval[interval_key] = True

    def _send_status_update(self, remaining_seconds):
        """Emits the current status of the countdown via SocketIO."""
        status = 'running'
        if remaining_seconds <= 0:
            status = 'finished'

        self.socketio.emit('countdown_update', {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'remaining_seconds': max(0, remaining_seconds),
            'formatted_time': self._format_seconds(max(0, remaining_seconds)),
            'status': status
        })

    def _handle_countdown_finished(self):
        """Called when the countdown reaches zero."""
        logging.info(f"Countdown '{self.name}' (ID: {self.id}) has finished.")
        self.stop() # Ensure the thread stops

    def _format_seconds(self, seconds):
        """Helper to format seconds into HH:MM:SS or MM:SS string."""
        if seconds < 0:
            seconds = 0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        # Add milliseconds for smoother UI updates if time is low
        milliseconds = int((seconds - int(seconds)) * 1000)

        if hours > 0:
            return f"{hours:02}:{minutes:02}:{secs:02}"
        if minutes > 0:
            return f"{minutes:02}:{secs:02}"
        return f"{secs:02}.{milliseconds:03}" # Show milliseconds when below 1 minute

    def stop(self):
        """Sets the stop event to terminate the countdown thread."""
        self._stop_event.set()
        logging.info(f"Stop signal sent for Countdown '{self.name}' (ID: {self.id}).")