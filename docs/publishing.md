# Publishing to YouTube, Instagram, and TikTok

ReelForge publishes finished reels straight to your accounts. Each platform
uses an OAuth app YOU create (all free), so there's a one-time setup per
platform. How the three differ:

| Platform | Upload path | What arrives |
|---|---|---|
| YouTube | direct resumable upload | video on your channel (private until your app passes Google verification) |
| Instagram | Meta FETCHES the video from a public URL (needs the tunnel) | Reel published publicly to your profile |
| TikTok | direct file upload | video in your TikTok inbox — you add the caption and post inside the TikTok app |

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
   **Multiple channels on one Google login?** Google authorizes ONE channel
   per connect — on its account-chooser screen, pick the channel you want.
   Then click **+ connect another channel** to add the rest. Each connected
   channel shows as a selectable chip, and the publish button always names
   the channel it will post to.
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
---

# Instagram setup (~15 minutes)

Requirements: an Instagram **Business or Creator** account (switch in the
Instagram app: Settings → Account type), and the tunnel (below).

1. Go to <https://developers.facebook.com/> → My Apps → **Create App** →
   use case "Other" → type **Business**.
2. In the app dashboard, **Add Product → Instagram** and choose
   "API setup with Instagram business login".
3. Under **Instagram → API setup with Instagram login → Business login
   settings**, add this OAuth redirect URI exactly:

       http://localhost:8001/api/v1/social/instagram/callback

4. Copy the **Instagram App ID** and **Instagram App Secret** (from the
   Instagram product page, not the general Meta app settings) into `.env`:

       INSTAGRAM_APP_ID=...
       INSTAGRAM_APP_SECRET=...

5. While the Meta app is in Development mode, add your Instagram account as
   an **Instagram Tester** (App roles → Roles) and accept the invite in the
   Instagram app (Settings → Apps and websites → Tester invites).
6. Start the tunnel (next section), then restart: `docker compose up -d api worker`.

## The tunnel (Instagram only)

Meta's servers must download your video from a public HTTPS URL — localhost
won't do. ReelForge ships a zero-account Cloudflare quick tunnel:

    docker compose --profile tunnel up -d tunnel
    docker compose logs tunnel 2>&1 | grep -o 'https://.*trycloudflare.com'

Put the printed URL in `.env`:

    REELFORGE_PUBLIC_MEDIA_BASE=https://<random>.trycloudflare.com

then `docker compose up -d worker`. The tunnel only exposes one tokened,
single-publication media URL while a publish is in flight — not your whole
library. **The URL changes every time the tunnel restarts**; re-do this step
when it does.

# TikTok setup (~15 minutes)

1. Go to <https://developers.tiktok.com/> → register as a developer →
   **Manage apps → Connect an app**.
2. Add the **Content Posting API** product and the **Login Kit**.
3. Request/enable the scopes `user.info.basic` and `video.upload`.
4. In sandbox/development, add your own TikTok account as a **target user**
   and set the redirect URI:

       http://localhost:8001/api/v1/social/tiktok/callback

   (If TikTok's console refuses a plain-http URI, run the tunnel from the
   Instagram section, set `REELFORGE_PUBLIC_API_BASE` to the tunnel URL, and
   register `https://<tunnel>/api/v1/social/tiktok/callback` instead.)
5. Copy the **Client key** and **Client secret** into `.env`:

       TIKTOK_CLIENT_KEY=...
       TIKTOK_CLIENT_SECRET=...

6. `docker compose up -d api worker`.

## TikTok behavior to know

- **Inbox flow, by design.** Unaudited TikTok apps can't post publicly.
  ReelForge uploads the video into your TikTok **inbox**: you get a
  notification in the app, tap it, add your caption, and post. Immediate,
  no audit needed, and you keep final control of the caption.
- **Max 5 pending inbox uploads per 24h.** Post or discard drafts in the
  app if you hit the limit (the error message will say so).
- Access tokens rotate on every publish; if publishing fails with a token
  error, disconnect + reconnect the account.
