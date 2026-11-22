"""Stand-in for onvif.py on the dev instance (server.ptz = "fake"): never touches the camera."""
CALLS = []  # ("move", pan, tilt) / ("stop",) in call order; read by tests


def profile_token():
    return "fake"


def move(pan=0.0, tilt=0.0, token=None):
    CALLS.append(("move", float(pan), float(tilt)))
    print(f"FAKE PTZ move {float(pan):.2f} {float(tilt):.2f}", flush=True)
    return "<ok/>"


def stop(token=None):
    CALLS.append(("stop",))
    print("FAKE PTZ stop", flush=True)
    return "<ok/>"
