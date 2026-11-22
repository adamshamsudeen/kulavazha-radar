"""Minimal ONVIF client (WS-Security UsernameToken) for the camera. Settings come from camlive.toml."""
import base64, datetime, hashlib, http.client, os, re, urllib.error, urllib.request
from xml.sax.saxutils import escape

import config

HOST = config.ONVIF_HOST
USER, PASS = config.CFG["camera"]["user"], config.CFG["camera"]["password"]
TIMEOUT = 3
NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
      'xmlns:tt="http://www.onvif.org/ver10/schema"')


def _security():
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASS.encode()).digest()).decode()
    return (
        '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{escape(USER)}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security>"
    )


def call(service, body):
    xml = f"<s:Envelope {NS}><s:Header>{_security()}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    req = urllib.request.Request(f"{HOST}/onvif/{service}", xml.encode(),
                                 {"Content-Type": "application/soap+xml"})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')}"
    except http.client.HTTPException as e:
        return f"HTTP 0: {e!r}"


def profile_token():
    found = re.findall(r'Profiles[^>]*token="([^"]*)"', call("media_service", "<trt:GetProfiles/>"))
    if not found:
        raise RuntimeError("no profile token")
    return found[0]


def move(pan=0.0, tilt=0.0, token=None):
    token = escape(token or profile_token())
    vel = f'<tt:PanTilt x="{float(pan):.2f}" y="{float(tilt):.2f}"/>'
    return call("ptz_service", f"<tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken>"
                               f"<tptz:Velocity>{vel}</tptz:Velocity></tptz:ContinuousMove>")


def stop(token=None):
    token = escape(token or profile_token())
    return call("ptz_service", f"<tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken>"
                               "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")
