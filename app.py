# app.py
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from countdown_manager import CountdownManager
from audio_manager import AudioManager
from vt_api_manager import OSCServerManager # Import the new OSC server manager
import logging
import os
import threading
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, static_folder='static', template_folder='static')
app.config['SECRET_KEY'] = 'your_secret_key_here' # IMPORTANT: Change this to a strong, random key
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Global Managers ---
# AudioManager is a singleton, so calling it here ensures it starts its thread.
audio_manager = AudioManager()

# Initialize OSCServerManager (for QLab replies)
# QLab typically replies on port 53001 if it receives on 53000.
# We'll make the listen port configurable via UI, but default to 53001 for replies.
# The actual QLab sending port (53000) will be passed to QLabAPIClient.
osc_server_manager = OSCServerManager(listen_ip="0.0.0.0", listen_port=53001) # Listen for QLab replies
osc_server_manager.start() # Start the OSC server thread

countdown_manager = CountdownManager(socketio, osc_server_manager) # Pass the OSC server manager

@app.route('/')
def index():
    """Renders the main web UI."""
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    """Called when a new client connects via SocketIO."""
    logging.info("Client connected.")
    # Send initial data about active countdowns to the newly connected client
    active_data = countdown_manager.get_all_active_countdown_data()
    emit('initial_countdown_data', active_data)

@socketio.on('disconnect')
def handle_disconnect():
    """Called when a client disconnects."""
    logging.info("Client disconnected.")

@socketio.on('start_item_countdown')
def start_item_countdown(data):
    """Handles request to start a specific item countdown."""
    name = data.get('name', 'Unnamed Item')
    target_time = data.get('target_time')
    verbalization_config = data.get('verbalization_config', {})

    logging.info(f"Received request to start item countdown: {name} at {target_time}")
    result = countdown_manager.start_item_countdown(name, target_time, verbalization_config)
    emit('countdown_started_response', result)

@socketio.on('start_vt_countdown')
def start_vt_countdown(data):
    """Handles request to start a video countdown."""
    name = data.get('name', 'Unnamed VT')
    vt_api_config = data.get('vt_api_config', {})
    verbalization_config = data.get('verbalization_config', {})

    logging.info(f"Received request to start VT countdown: {name} with API config: {vt_api_config}")
    result = countdown_manager.start_vt_countdown(name, vt_api_config, verbalization_config)
    emit('countdown_started_response', result)


@socketio.on('stop_countdown')
def stop_countdown(data):
    """Handles request to stop a specific countdown."""
    countdown_id = data.get('id')
    logging.info(f"Received request to stop countdown: {countdown_id}")
    if countdown_manager.stop_countdown(countdown_id):
        emit('countdown_stopped_response', {"success": True, "id": countdown_id})
    else:
        emit('countdown_stopped_response', {"success": False, "id": countdown_id, "message": "Countdown not found."})

@socketio.on('get_all_countdowns')
def get_all_countdowns():
    """Sends all active countdown data to the client."""
    active_data = countdown_manager.get_all_active_countdown_data()
    emit('initial_countdown_data', active_data)


def shutdown_server():
    """Shuts down the Flask server and cleans up countdowns/audio."""
    logging.info("Shutting down Flask server and managers...")
    countdown_manager.shutdown()
    osc_server_manager.stop() # Stop the OSC server
    logging.info("All managers shut down. Exiting.")
    # For a clean exit in a multi-threaded Flask-SocketIO app, os._exit(0) is often needed
    # especially if threads are not daemonized or don't exit cleanly on their own.
    os._exit(0)

if __name__ == '__main__':
    logging.info("Starting Flask-SocketIO server...")
    # Register shutdown hook
    import atexit
    atexit.register(shutdown_server)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)