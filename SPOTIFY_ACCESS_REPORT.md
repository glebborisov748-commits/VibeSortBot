# Spotify development access report — draft for the owner

Subject: Development Mode: OAuth succeeds, but GET /v1/me returns 403 after allowlisting

Application: VibeSort
Client ID: [paste the public Client ID ending in d7ecbd; do not include the Client Secret]

The app owner reports an active Spotify Premium subscription. The account email is present in Users Management, and the owner has checked that it matches the Spotify account used in the authorization flow. The Client ID used by the bot matches the Dashboard application.

Authorization Code with PKCE completes and token exchange succeeds. The token has the user-library-read scope. After reconnecting, both endpoints still fail:

- GET https://api.spotify.com/v1/me
- GET https://api.spotify.com/v1/me/tracks

HTTP status: 403
Message: Check settings on https://developer.spotify.com/dashboard, the user may not be registered.

Could you check whether this application's Development Mode registration and user allowlist are active on the API side, and advise on the cause? We have not identified a confirmed fix.

Similar publicly reported case (not proof of the same underlying cause):
https://community.spotify.com/forum/en/discussion/7559089/403-the-user-may-not-be-registered-on-v1-me-despite-user-allowlisted-in-development-mode

Never attach access tokens, refresh tokens, Client Secret, encryption keys, or an unredacted HAR. This draft has not been sent anywhere.
