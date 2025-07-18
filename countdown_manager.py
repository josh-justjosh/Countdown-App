# countdown_manager.py (UPDATED)
from countdown import Countdown
from audio_manager import AudioManager
import threading
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class CountdownManager:
    """Manages creation, tracking, and stopping of multiple countdown instances."""

    def __init__(self, socketio):
        self.active_countdowns = {}  # {countdown_id: Countdown_instance}
        self.socketio = socketio
        self.audio_manager = AudioManager() # Get the singleton instance
        self._next_id = 1
        self._lock = threading.Lock() # For thread-safe access to active_countdowns

    def _get_next_id(self):
        with self._lock:
            _id = self._next_id
            self._next_id += 1
            return _id

    def start_item_countdown(self, name: str, target_time_str: str, verbalization_config: dict) -> dict:
        """
        Starts a countdown for a specific item.
        target_time_str format: "YYYY-MM-DD HH:MM:SS"
        """
        try:
            target_datetime = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
            if target_datetime < datetime.now():
                logging.warning(f"Attempted to start item countdown '{name}' for a past time: {target_time_str}")
                return {"success": False, "message": "Target time must be in the future."}

            countdown_id = f"item_{self._get_next_id()}"
            countdown = Countdown(
                countdown_id=countdown_id,
                countdown_type='item',
                name=name,
                target_config=target_datetime, # Pass datetime object
                verbalization_settings=verbalization_config,
                audio_manager=self.audio_manager,
                socketio=self.socketio
            )
            with self._lock:
                self.active_countdowns[countdown_id] = countdown
            countdown.start()
            logging.info(f"Started item countdown: '{name}' (ID: {countdown_id}) until {target_datetime}")
            return {"success": True, "id": countdown_id, "name": name, "type": "item"}
        except ValueError as e:
            logging.error(f"Invalid datetime format for item countdown: {target_time_str}. Error: {e}")
            return {"success": False, "message": f"Invalid date/time format: {e}"}
        except Exception as e:
            logging.error(f"Error starting item countdown '{name}': {e}")
            return {"success": False, "message": f"An unexpected error occurred: {e}"}


    def start_vt_countdown(self, name: str, vt_api_config: dict, verbalization_config: dict) -> dict:
        """
        Starts a countdown for a video (VT) by polling QLab/vMix APIs.
        vt_api_config: dict containing 'qlab_ip', 'qlab_port', 'vmix_ip', 'vmix_port',
                                      'vmix_fallback1_input', 'vmix_fallback2_input'.
        """
        # Basic validation for API config
        if not (vt_api_config.get('qlab_ip') or vt_api_config.get('vmix_ip')):
            return {"success": False, "message": "At least one of QLab or vMix API details must be provided for VT countdown."}

        countdown_id = f"vt_{self._get_next_id()}"
        countdown = Countdown(
            countdown_id=countdown_id,
            countdown_type='vt',
            name=name,
            target_config=vt_api_config, # Pass API configuration dict
            verbalization_settings=verbalization_config,
            audio_manager=self.audio_manager,
            socketio=self.socketio
        )
        with self._lock:
            self.active_countdowns[countdown_id] = countdown
        countdown.start()
        logging.info(f"Started VT countdown: '{name}' (ID: {countdown_id}) with API polling.")
        return {"success": True, "id": countdown_id, "name": name, "type": "vt"}

    def stop_countdown(self, countdown_id: str):
        """Stops a specific countdown by its ID."""
        with self._lock:
            countdown = self.active_countdowns.pop(countdown_id, None)
            if countdown:
                countdown.stop()
                # Wait for the thread to actually finish to clean up resources
                countdown.join(timeout=1) # Give it 1 second to stop
                logging.info(f"Stopped countdown: {countdown_id}")
                # Send a final update to the UI to remove it
                self.socketio.emit('countdown_removed', {'id': countdown_id})
                return True
            logging.warning(f"Attempted to stop non-existent countdown: {countdown_id}")
            return False

    def get_all_active_countdown_data(self):
        """Returns current data for all active countdowns for initial UI load."""
        data = []
        with self._lock:
            for c_id, countdown in self.active_countdowns.items():
                if countdown.type == 'item':
                    remaining_seconds = (countdown.target_datetime - datetime.now()).total_seconds()
                elif countdown.type == 'vt':
                    # For VT, use the last known remaining seconds
                    remaining_seconds = countdown.last_known_remaining_seconds if countdown.last_known_remaining_seconds is not None else 0
                else:
                    remaining_seconds = 0 # Fallback

                data.append({
                    'id': countdown.id,
                    'name': countdown.name,
                    'type': countdown.type,
                    'remaining_seconds': max(0, remaining_seconds),
                    'formatted_time': countdown._format_seconds(max(0, remaining_seconds)),
                    'status': 'running' if remaining_seconds > 0 else 'finished'
                })
        return data

    def shutdown(self):
        """Stops all active countdowns and shuts down the audio manager."""
        logging.info("Shutting down CountdownManager and all active countdowns...")
        countdown_ids_to_stop = list(self.active_countdowns.keys()) # Copy keys to avoid RuntimeError during iteration
        for countdown_id in countdown_ids_to_stop:
            self.stop_countdown(countdown_id) # This will remove from active_countdowns
        self.audio_manager.shutdown()
        logging.info("CountdownManager shutdown complete.")