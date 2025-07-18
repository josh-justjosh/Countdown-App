# vt_api_manager.py (UPDATED)
import requests
import xml.etree.ElementTree as ET
from pythonosc import udp_client, osc_message_builder, osc_server
from pythonosc.dispatcher import Dispatcher
import threading
import time
import logging
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class OSCServerManager:
    """
    Manages an OSC server to listen for QLab replies and updates.
    Stores the latest QLab state in a thread-safe manner.
    """
    def __init__(self, listen_ip: str, listen_port: int):
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.dispatcher = Dispatcher()
        self.server = None
        self._stop_event = threading.Event()
        self._qlab_state = defaultdict(dict) # {cue_id: {property: value, ...}}
        self._state_lock = threading.Lock()
        self.server_thread = None

        # Map QLab reply addresses to handlers
        self.dispatcher.map("/reply/*", self._handle_qlab_reply)
        # QLab can also send /update messages if configured, though replies are more direct for queries.
        # For /update/workspace/{workspace_id}/cue_id/{cue_id}
        self.dispatcher.map("/update/workspace/*/cue_id/*", self._handle_qlab_update_cue_id)
        # For /update/workspace/{workspace_id}/cueList/{cue_list_id}/playbackPosition {cue_id}
        self.dispatcher.map("/update/workspace/*/cueList/*/playbackPosition", self._handle_qlab_update_playback_position)

        logging.info(f"OSC Server Manager initialized to listen on {listen_ip}:{listen_port}")

    def _handle_qlab_reply(self, address, *args):
        """
        Handler for QLab OSC replies to queries.
        Address format: /reply/{original_query_address}
        e.g., /reply/cue/active/uid, /reply/cue/{uid}/duration
        """
        logging.debug(f"OSC Reply received: {address}, Args: {args}")
        parts = address.split('/')
        
        # Example: /reply/cue/active/uid -> parts[3] = 'cue', parts[4] = 'active', parts[5] = 'uid'
        # Example: /reply/cue/{uid}/duration -> parts[3] = 'cue', parts[4] = '{uid}', parts[5] = 'duration'

        if len(parts) >= 6 and parts[1] == 'reply' and parts[2] == 'cue':
            if parts[4] == 'active' and parts[5] == 'uid':
                # This is a reply to /cue/active/uid?
                with self._state_lock:
                    # QLab's /cue/active/uid? replies with a list of currently active UIDs.
                    # We should update the 'active' status for these UIDs.
                    # It's safer to mark all previously active as inactive first, then update.
                    for uid in list(self._qlab_state.keys()): # Iterate over a copy
                        if self._qlab_state[uid].get('active'):
                            self._qlab_state[uid]['active'] = False # Mark as inactive unless found again
                    
                    for uid in args:
                        self._qlab_state[uid]['active'] = True
                        self._qlab_state[uid]['last_updated'] = time.time()
                logging.debug(f"QLab active UIDs updated: {args}")
            elif len(parts) == 6: # e.g., /reply/cue/{uid}/duration
                cue_id = parts[4]
                prop_name = parts[5]
                if args:
                    with self._state_lock:
                        self._qlab_state[cue_id][prop_name] = args[0]
                        self._qlab_state[cue_id]['last_updated'] = time.time()
                    logging.debug(f"QLab cue {cue_id} property {prop_name} updated to {args[0]}")
        else:
            logging.debug(f"Unhandled QLab reply address: {address}")

    def _handle_qlab_update_cue_id(self, address, workspace_id, cue_id):
        """Handler for /update/workspace/{workspace_id}/cue_id/{cue_id} messages."""
        logging.debug(f"OSC Update Cue ID received: {address}, Workspace: {workspace_id}, Cue ID: {cue_id}")
        # This update means the state of this specific cue has changed.
        # We might need to re-query its properties if it's relevant.
        # For now, we'll just log it. The polling mechanism will re-query.

    def _handle_qlab_update_playback_position(self, address, workspace_id, cue_list_id, playback_cue_id=None):
        """Handler for /update/workspace/{workspace_id}/cueList/{cue_list_id}/playbackPosition {cue_id}."""
        logging.debug(f"OSC Playback Position Update received: {address}, Playback Cue ID: {playback_cue_id}")
        with self._state_lock:
            # Mark all cues as not playhead
            for uid in list(self._qlab_state.keys()):
                if self._qlab_state[uid].get('is_playhead'):
                    self._qlab_state[uid]['is_playhead'] = False

            if playback_cue_id:
                self._qlab_state[playback_cue_id]['is_playhead'] = True
                self._qlab_state[playback_cue_id]['last_updated'] = time.time()
        logging.debug(f"QLab playback position updated to: {playback_cue_id}")


    def start(self):
        """Starts the OSC server in a daemon thread."""
        try:
            self.server = osc_server.ThreadingOSCUDPServer(
                (self.listen_ip, self.listen_port), self.dispatcher
            )
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            logging.info(f"OSC Server started on {self.listen_ip}:{self.listen_port}")
        except Exception as e:
            logging.error(f"Failed to start OSC server: {e}")
            self.server = None

    def stop(self):
        """Shuts down the OSC server."""
        if self.server:
            logging.info("Shutting down OSC server...")
            self.server.shutdown()
            self.server.server_close()
            if self.server_thread and self.server_thread.is_alive():
                self.server_thread.join(timeout=1)
            logging.info("OSC Server shut down.")

    def get_qlab_state(self):
        """Returns a thread-safe copy of the current QLab state."""
        with self._state_lock:
            return dict(self._qlab_state) # Return a copy to prevent external modification issues


class QLabAPIClient:
    """Handles sending OSC messages to QLab and reading state from OSCServerManager."""
    def __init__(self, ip: str, port: int, osc_server_manager: OSCServerManager):
        self.ip = ip
        self.port = port
        self.client = None
        self.osc_server_manager = osc_server_manager # Reference to the shared OSC server
        try:
            self.client = udp_client.SimpleUDPClient(self.ip, self.port)
            logging.info(f"QLab OSC client initialized for {ip}:{port}")
        except Exception as e:
            logging.error(f"Failed to initialize QLab OSC client for {ip}:{port}: {e}")
            self.client = None

    def _send_osc_query(self, address, *args):
        """Helper to send an OSC query to QLab."""
        if not self.client:
            logging.warning(f"QLab client not initialized. Cannot send OSC query to {address}.")
            return False
        try:
            msg = osc_message_builder.OscMessageBuilder(address=address)
            for arg in args:
                msg.add_arg(arg)
            self.client.send(msg.build())
            logging.debug(f"Sent OSC query to QLab: {address} {args}")
            return True
        except Exception as e:
            logging.warning(f"Failed to send OSC query to QLab ({address}): {e}")
            return False

    def get_active_video_remaining_time(self) -> tuple[float | None, str | None]:
        """
        Queries QLab for active video cues and calculates remaining time.
        This relies on the OSCServerManager receiving replies from QLab.
        """
        if not self.client or not self.osc_server_manager:
            return None, None

        # 1. Request active cue UIDs from QLab
        # This will trigger /reply/cue/active/uid to the OSCServerManager
        self._send_osc_query("/cue/active/uid", "?")
        # Also request playhead position to prioritize
        self._send_osc_query("/cue/playbackPosition", "?")
        
        # Give QLab a moment to reply and the OSC server to process
        time.sleep(0.05) 

        qlab_state = self.osc_server_manager.get_qlab_state()
        
        # Prioritize playhead if it's a video cue
        playhead_uid = None
        for uid, data in qlab_state.items():
            if data.get('is_playhead'):
                playhead_uid = uid
                break

        target_uids = []
        if playhead_uid:
            target_uids.append(playhead_uid)
        
        # Add other active UIDs if not already the playhead
        active_uids = [uid for uid, data in qlab_state.items() if data.get('active') and uid not in target_uids]
        target_uids.extend(active_uids)

        if not target_uids:
            logging.debug("QLab: No active or playhead cues reported.")
            return None, None

        # 2. For each relevant UID, query its type, duration, and elapsed time
        for uid in target_uids:
            self._send_osc_query(f"/cue/{uid}/type", "?")
            self._send_osc_query(f"/cue/{uid}/duration", "?")
            self._send_osc_query(f"/cue/{uid}/actionElapsed", "?")
            self._send_osc_query(f"/cue/{uid}/name", "?") # Also get name
            time.sleep(0.02) # Small delay for replies for each query

        # 3. Re-read state and find a suitable video cue
        qlab_state = self.osc_server_manager.get_qlab_state()
        
        for uid in target_uids:
            cue_data = qlab_state.get(uid, {})
            cue_type = cue_data.get('type')
            duration = cue_data.get('duration')
            elapsed = cue_data.get('actionElapsed')
            cue_name = cue_data.get('name', f"QLab Cue {uid[:8]}...")
            
            # Check if it's a video cue and has valid duration/elapsed time
            # QLab video cue types are typically "Video"
            if cue_type == "Video" and isinstance(duration, (int, float)) and isinstance(elapsed, (int, float)):
                if duration > 0:
                    remaining_s = duration - elapsed
                    logging.debug(f"QLab found active video cue: {cue_name}, Remaining: {remaining_s:.2f}s")
                    return max(0.0, remaining_s), cue_name
                else:
                    logging.debug(f"QLab cue {uid} is 'Video' type but has zero or invalid duration.")

        logging.debug("QLab: No active video cue with valid duration/elapsed found among active/playhead cues.")
        return None, None


class VmixAPIClient:
    """Handles communication with vMix via HTTP XML API."""
    def __init__(self, ip: str, port: int):
        self.base_url = f"http://{ip}:{port}/api"
        logging.info(f"vMix API client initialized for {self.base_url}")

    def get_status_xml(self) -> ET.Element | None:
        """Fetches the current vMix status XML."""
        try:
            response = requests.get(self.base_url)
            response.raise_for_status() # Raise an exception for HTTP errors
            root = ET.fromstring(response.content)
            return root
        except requests.exceptions.ConnectionError as e:
            logging.error(f"vMix Connection Error: {e}. Is vMix running and API enabled at {self.base_url}?")
            return None
        except requests.exceptions.Timeout:
            logging.error(f"vMix Request Timeout: {self.base_url}")
            return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching vMix XML status from {self.base_url}: {e}")
            return None
        except ET.ParseError as e:
            logging.error(f"Error parsing vMix XML response: {e}")
            return None

    def get_video_input_status(self, xml_root: ET.Element, input_identifier: str | int) -> tuple[float | None, str | None]:
        """
        Parses XML for a specific video input and returns its remaining time and title.
        input_identifier can be input number (str or int) or title (str).
        """
        try:
            # Find the target input
            target_input = None
            for input_elem in xml_root.findall(".//input"):
                num = input_elem.get("number")
                title = input_elem.get("title")

                if isinstance(input_identifier, int) and num == str(input_identifier):
                    target_input = input_elem
                    break
                elif isinstance(input_identifier, str) and title == input_identifier:
                    target_input = input_elem
                    break

            if target_input is None:
                logging.debug(f"vMix: Input '{input_identifier}' not found in XML.")
                return None, None

            input_type = target_input.get("type")
            # Check if it's a video-like input type
            # Common video types in vMix XML: "Video", "VideoList", "GT", "NDI", "DeckLink"
            # We focus on types that have a clear duration/position
            if input_type not in ["Video", "VideoList", "GT", "Stream", "Replay", "DeckLink", "ImageSequence", "VideoDelay"]:
                 logging.debug(f"vMix: Input '{input_identifier}' (type: {input_type}) is not a recognized video type with duration.")
                 return None, None

            duration_ms = int(target_input.get("duration", "0"))
            position_ms = int(target_input.get("position", "0"))
            title = target_input.get("title", f"Input {input_identifier}")

            if duration_ms <= 0:
                logging.debug(f"vMix: Video input '{title}' (ID: {input_identifier}) has zero or invalid duration.")
                return None, None

            remaining_ms = duration_ms - position_ms
            remaining_s = remaining_ms / 1000.0

            if remaining_s < 0: # Can happen if video has finished but API hasn't updated quickly
                remaining_s = 0

            return remaining_s, title

        except Exception as e:
            logging.warning(f"Error parsing vMix input status for '{input_identifier}': {e}")
            return None, None

    def get_program_video_remaining_time(self, xml_root: ET.Element) -> tuple[float | None, str | None]:
        """
        Finds the video input currently in the Program output and returns its remaining time.
        """
        program_input_number = None
        # Iterate through mixes to find the main program mix (number="1")
        for mix_elem in xml_root.findall(".//mix"):
            if mix_elem.get("number") == "1": # Main mix (program)
                # Find the active input within this mix
                for input_elem in mix_elem.findall("./input"):
                    if input_elem.get("active") == "true": # This input is active on program
                        program_input_number = input_elem.get("number") # Get its number
                        break
            if program_input_number:
                break

        if not program_input_number:
            logging.debug("vMix: No active input found in Program output.")
            return None, None

        # Now get the full status for this input using its number
        # We need to iterate through all inputs again to find the one matching the program_input_number
        # as the mix element only gives a subset of input attributes.
        return self.get_video_input_status(xml_root, program_input_number)
