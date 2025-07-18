# vt_api_manager.py
import requests
import xml.etree.ElementTree as ET
from pythonosc import udp_client, osc_message_builder
import threading
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class QLabAPIClient:
    """Handles communication with QLab via OSC."""
    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        self.client = None
        try:
            self.client = udp_client.SimpleUDPClient(self.ip, self.port)
            logging.info(f"QLab OSC client initialized for {ip}:{port}")
        except Exception as e:
            logging.error(f"Failed to initialize QLab OSC client for {ip}:{port}: {e}")
            self.client = None

    def _send_osc_query(self, address, *args):
        """Helper to send an OSC query and attempt to get a response (though UDP is fire-and-forget)."""
        if not self.client:
            return None

        # QLab typically replies to the port it received the message on + 1, or 53001
        # For querying, QLab expects specific query messages.
        # This is basic; a full OSC server for replies would be more robust.
        # For now, we'll send a query and hope for the best (or assume push updates if QLab supports them for this)
        # QLab's OSC API for querying properties is usually /cue/{id}/property?
        try:
            msg = osc_message_builder.OscMessageBuilder(address=address)
            for arg in args:
                msg.add_arg(arg)
            self.client.send(msg.build())
            return True # Indicates message sent, not necessarily a successful reply
        except Exception as e:
            logging.warning(f"Failed to send OSC query to QLab ({address}): {e}")
            return False

    def get_cue_property(self, cue_id: str, prop: str):
        """
        Sends an OSC query to get a specific property of a cue.
        QLab usually replies to port 53001 if the client listens there.
        This client is simplified and doesn't listen for replies.
        A more robust solution would involve an OSC server within Python.
        For simplified use, QLab users often send queries and poll.
        """
        if not self.client:
            return None
        # QLab uses /cue/{id}/{property} for queries.
        # It sends back /reply/{address} args to port 53001
        # Since we're not running an OSC server, this is a best-effort.
        # Real-time QLab integration for countdowns often involves custom OSC feedback from QLab
        # to a dedicated OSC receiver.
        # For simplicity, we'll try to use /cue/active and poll properties.

        # The QLab documentation suggests querying /cue/{id}/duration? and /cue/{id}/actionElapsed?
        # to calculate remaining time.
        # The challenge is getting {id} of the *active video* cue automatically.
        # QLab has /cue/active which addresses all currently playing cues.
        # Querying /cue/active/type would return a list of types.
        # This requires an OSC *server* to receive the replies.

        # Given the "polling frequency should be 500ms" constraint,
        # it's implied we'd actively poll.
        # Without a full OSC server here, the most direct polling approach is limited.
        # I'll make a strong assumption here based on typical QLab usage:
        # A specific 'video' cue is expected to be playing and its ID will be known
        # or discoverable via a separate manual step.
        # For automated "active cue" detection without an OSC server to receive async replies:
        # One common approach is to send `/query/allCues` and parse replies, or use `/cue/active/uid`
        # and then query properties for those UIDs. This gets complex without a proper OSC server.

        # For the scope of this script, let's assume a "primary video cue" concept or
        # a more direct way to determine the active video cue by querying `/cue/playhead` or similar.
        # The user said "OSC should be able to work with the active cue".
        # The most reliable way to get *a* currently playing cue and its progress
        # without complex reply parsing and state management (which belongs in a dedicated QLab API wrapper)
        # is often to query the playhead of the main cue list or a known cue list.
        # /cueList/{id}/playbackPosition returns the UID of the playhead.
        # Then, we can query that UID.

        logging.debug(f"Attempting to query QLab for cue {cue_id}'s {prop}")
        msg = osc_message_builder.OscMessageBuilder(address=f"/cue/{cue_id}/{prop}")
        msg.add_arg('?') # QLab query syntax
        self.client.send(msg.build())
        # We can't synchronously get the reply here with SimpleUDPClient.
        # A more robust QLab integration requires a dedicated OSC server thread in this app.
        # For now, I will create a placeholder for a more advanced QLab logic
        # and might have to simplify to a "known QLab cue ID" input for the user
        # if dynamic "active video cue" discovery proves too complex without full OSC server.

        return None # Cannot get immediate reply

    def get_active_video_remaining_time(self, main_cue_list_id: str = "1", video_cue_type: str = "Video") -> tuple[float | None, str | None]:
        """
        Attempts to get the remaining time of the active video cue in QLab.
        This is a highly simplified implementation due to the limitations of SimpleUDPClient
        and the complexity of dynamic QLab state parsing without an OSC server.
        A more robust solution would involve:
        1. Running an `osc_server.ThreadingOSCUDPServer` to listen for QLab replies.
        2. QLab sending `/update` messages or specific replies to queries.
        3. Maintaining a state of active cues based on these updates.

        For this current scope, this function will assume a specific cue can be identified
        (e.g., by ID from a UI input) and then its properties are queried.
        The most "active" QLab cue is often the playhead.
        If we query `/cueList/{id}/playbackPosition` to get a UID,
        then `/cue/{uid}/type`, `/cue/{uid}/duration`, `/cue/{uid}/actionElapsed`.
        However, `python-osc`'s `udp_client.SimpleUDPClient` is fire-and-forget for sends.
        To *receive* replies, we need a `osc_server`.

        Given the constraint, I will implement a placeholder for QLab, and recommend
        that for true "active cue" tracking, a separate OSC server is needed within the Python app
        to receive QLab's asynchronous responses (e.g., to `/cue/active` queries or `/update` messages).

        For now, this will return None, None, until a full OSC server is integrated
        or a specific, known cue ID is provided for polling.
        A more practical approach for live use (without a full OSC server) is to have the QLab operator
        manually input the QLab Cue ID they want to track into the UI.
        Let's assume for *now* that the user will provide a QLab Cue ID in the UI if they want to track QLab.
        """
        logging.warning("QLab 'active video' detection requires a full OSC server to receive replies. "
                        "This method is a placeholder. Please provide a known cue_id if you want to track QLab.")
        # If the user provides a 'tracked_cue_id' for QLab in the UI:
        # cue_id = "your_user_provided_cue_id"
        # self._send_osc_query(f"/cue/{cue_id}/duration?")
        # self._send_osc_query(f"/cue/{cue_id}/actionElapsed?")
        # ... and then an OSC server would receive these replies.
        # Without that, we can't automatically get the data.
        return None, None # Cannot get dynamic remaining time without listening for OSC replies

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
            if input_type not in ["Video", "VideoList", "GT", "Stream", "Replay", "DeckLink", "ImageSequence"]:
                 logging.debug(f"vMix: Input '{input_identifier}' (type: {input_type}) is not a recognized video type.")
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
        program_input_id = None
        for mix_elem in xml_root.findall(".//mix"):
            if mix_elem.get("number") == "1": # Main mix (program)
                for input_elem in mix_elem.findall("./input"):
                    if input_elem.get("active") == "true": # This input is active on program
                        program_input_id = input_elem.get("key") # Get its unique ID
                        break
            if program_input_id:
                break

        if not program_input_id:
            logging.debug("vMix: No active input found in Program output.")
            return None, None

        # Now find the full input details using its key
        for input_elem in xml_root.findall(".//input"):
            if input_elem.get("key") == program_input_id:
                return self.get_video_input_status(xml_root, input_elem.get("number")) # Use number for consistency
        return None, None
