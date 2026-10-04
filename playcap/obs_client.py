"""Minimal obs-websocket v5 client -- only what the recorder needs.

Protocol: server sends Hello (op 0) with an auth challenge, we reply with
Identify (op 1) carrying base64(sha256(base64(sha256(password+salt)) + challenge)),
server confirms with Identified (op 2). After that it's request (op 6) /
response (op 7) pairs matched by requestId.
"""
import base64
import hashlib
import json
import time

import websocket

DEFAULT_URL = "ws://127.0.0.1:4455"


class ObsError(RuntimeError):
    pass


class Obs:
    def __init__(self, password, url=DEFAULT_URL, timeout=10):
        try:
            self.ws = websocket.create_connection(url, timeout=timeout)
        except Exception as exc:
            raise ObsError(
                f"Cannot reach obs-websocket at {url}: {exc}\n"
                "In OBS: Tools -> WebSocket Server Settings -> "
                "tick 'Enable WebSocket server' -> Apply."
            ) from exc
        self.req_n = 0
        self._identify(password)

    def _recv(self):
        return json.loads(self.ws.recv())

    def _identify(self, password):
        hello = self._recv()
        auth = hello["d"].get("authentication")
        payload = {"rpcVersion": 1, "eventSubscriptions": 0}
        if auth:
            secret = base64.b64encode(
                hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            payload["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self.ws.send(json.dumps({"op": 1, "d": payload}))
        reply = self._recv()
        if reply.get("op") != 2:
            raise ObsError(f"OBS refused identify (wrong password?): {reply}")

    def request(self, request_type, data=None, timeout=20):
        self.req_n += 1
        req_id = f"r{self.req_n}"
        self.ws.send(json.dumps({"op": 6, "d": {
            "requestType": request_type, "requestId": req_id,
            "requestData": data or {}}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            msg = self._recv()
            if msg.get("op") == 7 and msg["d"]["requestId"] == req_id:
                status = msg["d"]["requestStatus"]
                if not status["result"]:
                    raise ObsError(
                        f"{request_type} failed: {status.get('comment') or status}")
                return msg["d"].get("responseData") or {}
        raise ObsError(f"{request_type} timed out")

    # --- the handful of calls the recorder actually makes -------------------
    def version(self):
        return self.request("GetVersion")

    def record_status(self):
        return self.request("GetRecordStatus")

    def start_record(self):
        if self.record_status()["outputActive"]:
            raise ObsError("OBS is already recording; refusing to start another.")
        self.request("StartRecord")

    def stop_record(self):
        """Returns the path OBS wrote."""
        return self.request("StopRecord").get("outputPath")

    def current_scene(self):
        return self.request("GetCurrentProgramScene").get("currentProgramSceneName")

    def scene_items(self, scene):
        return self.request("GetSceneItemList", {"sceneName": scene})["sceneItems"]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
