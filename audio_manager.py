# audio_manager.py
import pyttsx3
import threading
import queue
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AudioManager:
    """Manages text-to-speech and prioritizes audio announcements."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern to ensure only one AudioManager instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AudioManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.engine = None
        self.audio_queue = queue.PriorityQueue() # (priority, timestamp, message)
        self.playback_thread = None
        self._stop_event = threading.Event()
        self._initialized = False # Flag to prevent re-initialization

        try:
            self.engine = pyttsx3.init()
            # Optional: Configure voice, speed, volume
            # voices = self.engine.getProperty('voices')
            # self.engine.setProperty('voice', voices[0].id) # Select a voice
            # self.engine.setProperty('rate', 175) # Speed of speech
            # self.engine.setProperty('volume', 1.0) # Volume (0.0 to 1.0)
            logging.info("pyttsx3 engine initialized successfully.")
            self._initialized = True
        except Exception as e:
            logging.error(f"Failed to initialize pyttsx3 engine: {e}")
            logging.warning("Audio announcements will not be available.")
            self._initialized = False

        if self._initialized:
            self.playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
            self.playback_thread.start()
            logging.info("Audio playback thread started.")

    def _playback_loop(self):
        """
        The main loop for the audio playback thread.
        Pulls messages from the priority queue and speaks them.
        """
        while not self._stop_event.is_set():
            try:
                # Get the highest priority message (lowest priority number)
                # timeout ensures the thread can be stopped gracefully
                priority, timestamp, message = self.audio_queue.get(timeout=0.1)
                logging.info(f"Speaking (Prio: {priority}): '{message}'")
                if self.engine:
                    self.engine.say(message)
                    self.engine.runAndWait()
                self.audio_queue.task_done()
            except queue.Empty:
                # No messages, just continue to check stop event
                pass
            except Exception as e:
                logging.error(f"Error during audio playback: {e}")
                # In case of an error, still mark task as done if it was retrieved
                if 'message' in locals():
                    self.audio_queue.task_done()
                time.sleep(0.5) # Prevent tight loop on persistent error

    def speak(self, message: str, remaining_time_seconds: float):
        """
        Adds a message to the audio queue with a priority based on remaining time.
        Lower remaining_time_seconds results in higher priority (lower priority value).
        """
        if not self._initialized:
            logging.warning(f"Audio manager not initialized. Cannot speak: {message}")
            return

        # Priority calculation: The shorter the remaining time, the higher the priority.
        # We want lower numbers for higher priority in PriorityQueue.
        # So, priority = -remaining_time_seconds (or just remaining_time_seconds if we sort descending)
        # Using remaining_time_seconds directly as priority means shorter time = lower priority number.
        # Add a timestamp to break ties (FIFO for same priority)
        priority = remaining_time_seconds
        self.audio_queue.put((priority, time.time(), message))
        logging.info(f"Queued message: '{message}' with priority {priority} (remaining time: {remaining_time_seconds}s)")

    def shutdown(self):
        """Gracefully shuts down the audio manager."""
        if self._initialized:
            logging.info("Shutting down audio manager...")
            self._stop_event.set()
            if self.playback_thread and self.playback_thread.is_alive():
                self.playback_thread.join(timeout=2) # Give it some time to finish
            if self.engine:
                self.engine.stop()
            self._initialized = False
            logging.info("Audio manager shut down.")

# Example usage (for testing, won't be run directly in Flask app)
if __name__ == "__main__":
    audio_mgr = AudioManager()

    print("Adding some messages to the queue...")
    audio_mgr.speak("Ten seconds!", 10)
    audio_mgr.speak("One minute remaining.", 60)
    audio_mgr.speak("Five seconds!", 5)
    audio_mgr.speak("Thirty seconds left.", 30)
    audio_mgr.speak("Two minutes to go.", 120)

    # Let some time pass for messages to be spoken
    time.sleep(15)

    print("Adding more messages, some with higher priority now...")
    audio_mgr.speak("Critical! One second!", 1)
    audio_mgr.speak("Almost there, three seconds!", 3)
    audio_mgr.speak("New event in 20 seconds!", 20)

    time.sleep(10)

    print("Done. Shutting down.")
    audio_mgr.shutdown()