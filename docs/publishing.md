# Publishing to YouTube

ReelForge can upload finished reels straight to your YouTube channel. It uses
your own Google Cloud OAuth client, so a one-time setup is required.

## One-time Google Cloud setup (~10 minutes)

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **Enable the API**: APIs & Services → Library → search "YouTube Data API v3"
   → Enable.
3. **OAuth consent screen**: APIs & Services → OAuth consent screen →
   External → fill in the app name + your email. Under **Test users**, add
   the Google account that owns your YouTube channel. (You do NOT need to
   submit for verification for personal use.)
4. **Credentials**: APIs & Services → Credentials → Create credentials →
   OAuth client ID → Application type **Web application**. Add this
   authorized redirect URI exactly:

       http://localhost:8001/api/v1/social/youtube/callback

5. Copy the client ID + secret into your `.env`:

       GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
       GOOGLE_CLIENT_SECRET=GOCSPX-...

6. Restart the stack: `docker compose up -d api worker`.

## Connecting and publishing

1. Open a reel in the web UI. Below the export grid there's a **Publish**
   card — click **Connect YouTube** and approve the consent screen.
2. Export the reel with the **MP4 · H.264 (social)** preset (publishing
   uploads that file).
3. Fill in title / description / visibility and hit **Publish to YouTube**.
   Progress streams live; when it completes you get a **View** link.

## Things to know

- **Unverified-app cap**: until your OAuth app passes Google verification,
  YouTube locks videos uploaded through it to **private**. For personal use
  that's usually fine — flip visibility in YouTube Studio, or go through
  verification if you need direct public publishing.
- **Quota**: the YouTube Data API grants 10,000 units/day by default; one
  upload costs ~1,600 units, so ~6 uploads/day. Request more quota in the
  Google Cloud console if you need it.
- Tokens are stored in the local SQLite DB (`/data/reelforge.db`) on your
  machine. **Disconnect** (in the Publish card) removes them; you can also
  revoke access at <https://myaccount.google.com/permissions>.
- Instagram and TikTok publishing are planned; both need platform app
  approvals with heavier requirements (Business account + public media URL
  for Instagram; app review for TikTok).
