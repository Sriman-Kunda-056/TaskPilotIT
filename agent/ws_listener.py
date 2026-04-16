"""
agent/ws_listener.py  –  Listens to panel SocketIO events for state confirmation.
"""
import threading, time
import socketio as sio_client


class PanelEventListener:
    def __init__(self, url="http://localhost:5000"):
        self.url    = url
        self.events = []
        self._sio   = sio_client.Client(logger=False, engineio_logger=False)

        @self._sio.on("action_result")
        def on_result(d):
            self.events.append(d)
            print(f"[WS] {'✅' if d.get('success') else '❌'} {d.get('event')} {d}")

        @self._sio.on("connect")
        def on_connect(): print(f"[WS] Connected → {self.url}")

    def start(self):
        def _run():
            try:
                self._sio.connect(self.url)
                self._sio.wait()
            except Exception as e:
                print(f"[WS] {e}")
        threading.Thread(target=_run, daemon=True).start()
        time.sleep(1.5)

    def stop(self):
        try: self._sio.disconnect()
        except: pass

    def latest(self): return self.events[-1] if self.events else None
    def clear(self):  self.events.clear()

    def wait_for(self, event_name, timeout=15.0):
        start, seen = time.time(), len(self.events)
        while time.time()-start < timeout:
            for ev in self.events[seen:]:
                if ev.get("event") == event_name: return ev
            time.sleep(0.3)
        return None
