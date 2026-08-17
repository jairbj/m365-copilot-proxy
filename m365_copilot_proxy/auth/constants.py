"""OAuth constants for the Microsoft 365 Copilot web client.

These are not ours to choose. `CLIENT_ID` is Microsoft's own first-party Office
Web Copilot application, which is the only client preauthorized for the Sydney
scopes below — nobody in the loop (not us, not a tenant admin) can register a new
one or add a redirect URI to it. Two consequences, both established the hard way
by the reference implementation and NOT worth rediscovering:

  * Device code flow fails at redemption with AADSTS7000218 (it demands a
    `client_secret` a public client can never hold).
  * A generated `http://localhost:<port>` callback is rejected with AADSTS50011.

`nativeclient` is already registered on the app, so it is the only door.
"""

from __future__ import annotations

CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
AUTHORITY = "https://login.microsoftonline.com/common"
REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"

#: Scopes for the BizChat conversation itself. The resulting token carries
#: audience `https://substrate.office.com/sydney`.
CHAT_SCOPES = [
    "https://substrate.office.com/sydney/M365Chat.Read",
    "https://substrate.office.com/sydney/sydney.readwrite",
]

#: Generated-image bytes live on designerapp.officeapps.live.com and 401 for the
#: Sydney token — the artifact wants a token for the designerapp service itself.
#: The same first-party client is preauthorized for it, so a silent acquisition
#: off the cached refresh token works.
IMAGE_SCOPES = ["https://designerappservice.officeapps.live.com/.default"]
