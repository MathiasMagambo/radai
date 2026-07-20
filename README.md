#### RADAI
Radai is an attempt to create a radio experience using podcasts and Spotify. If you like listening to podcasts, love listening to music and want to do both at the same time, I've made this for you.
It's a private, always-on internet radio station that combines prepared YouTube podcasts with Spotify music breaks. It runs on your server, streams through the web interface, and keeps running even after the browser is closed.

### HOW IT WORKS
The podcast is pulled from Youtube with a transcript. The transcript is run through an AI model of your choice (currently DeepSeek, will configure other models later) to generate cut timetamps where ads are or based on topics. 
Music gets played in between the podcast ad breaks using Spotify. 
The Radai engine controls Spotify installed on your server through the GUI. spotifyd is used as an audio capture device. Like having Bluetooth headphones for your server to be able to listen to audio.   
 ```text
   YouTube channel/video
     → yt-dlp downloads audio and subtitles
     → AI model identifies ads and music-break positions
     → FFmpeg removes ads and prepares normalized podcast audio
     → Radai Engine plays podcast segments
                                     │
    Spotify Desktop → spotifyd PCM ──┤
                                     ▼
                            FFmpeg MP3 encoder
                                     ▼
                              Icecast source
                                     ▼
                       Radai buffered web stream
                                     ▼
                         Nginx + browser/VLC
 ```

### FEATURES
This is only for the main features and updates. Everything else is in FEATURES.md
- Add links to YouTube channels as podcast sources
- Add extra music breaks as you listen to a podcast
- 

### INTERFACE
<img width="1833" height="930" alt="image" src="https://github.com/user-attachments/assets/81bb0120-5252-4cde-bf87-41d9b7e7141a" />

 ### Audio and web stack

 - Python 3 backend
 - Python standard-library ThreadingHTTPServer
 - Vanilla HTML, CSS and JavaScript frontend; no frontend build step
 - FFmpeg/ffprobe for decoding, editing and MP3 encoding
 - Icecast as the local MP3 source server
 - Optional Node.js runtime for newer YouTube JavaScript challenges

 Runtime state, downloaded media, transcripts, prepared audio and authentication caches live under data/ and are excluded from Git.

 ────────────────────────────────────────────────────────────────────────────────

 ### INSTALLATION

 The following example assumes Ubuntu and installs the project at /opt/radai.

 1. Install system packages

 ```bash
   sudo apt update
   sudo apt install \
     git \
     python3 \
     python3-venv \
     ffmpeg \
     icecast2 \
     nginx
 ```

 Optional HTTPS support:

 ```bash
   sudo apt install certbot python3-certbot-nginx
 ```

 Install Spotify Desktop. For example, using the official Snap:

 ```bash
   sudo snap install spotify
 ```

 Install spotifyd (https://docs.spotifyd.rs/installation/index.html) using your distribution package or one of its release binaries (https://github.com/Spotifyd/spotifyd/releases). Confirm it is available:

 ```bash
   spotifyd --version
 ```

 A Spotify Premium account is normally required for Spotify Connect playback through spotifyd.

 2. Clone and install Radai Engine

 ```bash
   sudo git clone https://github.com/MathiasMagambo/radai /opt/radai
   sudo chown -R "$USER:$USER" /opt/radai

   cd /opt/radai
   python3 -m venv .venv
   .venv/bin/pip install --upgrade pip
   .venv/bin/pip install -e .
   .venv/bin/pip install yt-dlp
 ```

 For development:

 ```bash
   .venv/bin/pip install -e '.[dev]'
   .venv/bin/pytest
 ```

 3. Configure the environment

 ```bash
   cd /opt/radai
   cp .env.example .env
   chmod 600 .env
 ```

 At minimum, configure:

 ```env
   RADAI_ROOT=/opt/radai
   RADIO_HOST=127.0.0.1
   RADIO_PORT=8090

   RADIO_USERNAME=radio
   RADIO_PASSWORD=replace-with-a-long-random-password

   DEEPSEEK_API_KEY=your-deepseek-api-key
   DEEPSEEK_MODEL=deepseek-chat

   ICECAST_SOURCE_PASSWORD=replace-with-another-random-password
   ICECAST_PORT=8001

   SPOTIFY_DEVICE_NAME=Radai Radio
   SPOTIFY_CDP_URL=http://127.0.0.1:9223
   SPOTIFYD_PATH=/home/your-user/.local/bin/spotifyd
 ```

 The legacy Spotify Web API fields are not required for the current Spotify Desktop/Song Radio workflow.

 4. Configure Icecast

 Edit:

 ```text
   /etc/icecast2/icecast.xml
 ```

 Set its source password to exactly the same value as:

 ```env
   ICECAST_SOURCE_PASSWORD=
 ```

 Configure Icecast to listen locally on port 8001. Restart it:

 ```bash
   sudo systemctl restart icecast2
   sudo systemctl status icecast2
 ```

 Radai publishes its source at:

 ```text
   http://127.0.0.1:8001/spotify.mp3
 ```

 Icecast does not need to be exposed publicly.

 5. Authenticate Spotify

 Run Spotify Desktop as the same Linux user that will run Radai. Sign into your Spotify account once.

 Authenticate spotifyd as that same user:

 ```bash
   spotifyd authenticate
 ```

 Follow the browser authorization link. spotifyd stores its OAuth credentials in its user cache. See the spotifyd authentication documentation (https://docs.spotifyd.rs/configuration/auth.html).

 6. Start Spotify Desktop with remote debugging

 Copy the supplied service:

 ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/radai-spotify-desktop.service ~/.config/systemd/user/
 ```

 Edit the copied unit if necessary:

 - Change /usr/bin/spotify to /snap/bin/spotify for Snap.
 - Set the correct DISPLAY.
 - Keep remote debugging on port 9223.

 Then enable it:

 ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now radai-spotify-desktop.service
 ```

 Check it:

 ```bash
   systemctl --user status radai-spotify-desktop.service
   curl http://127.0.0.1:9223/json
 ```

 A headless server needs a persistent graphical session, such as Xvfb, with the service’s DISPLAY pointed at that session.

 7. Install the Radai web service

 ```bash
   cp deploy/radai-web.service ~/.config/systemd/user/
 ```

 Adjust /opt/radai in the unit if the repository is installed elsewhere.

 Enable it:

 ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now radai-web.service
 ```

 For startup without an interactive SSH login:

 ```bash
   sudo loginctl enable-linger "$USER"
 ```

 Verify:

 ```bash
   systemctl --user status radai-web.service
   curl -u 'radio:your-password' http://127.0.0.1:8090/api/status
 ```

 8. Configure Nginx and HTTPS

 Copy the supplied Nginx template:

 ```bash
   sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/radai
   sudo ln -s /etc/nginx/sites-available/radai /etc/nginx/sites-enabled/radai
 ```

 Edit server_name and replace radio.example.com with your domain.

 Validate and reload:

 ```bash
   sudo nginx -t
   sudo systemctl reload nginx
 ```

 For HTTPS:

 ```bash
   sudo certbot --nginx -d radio.example.com
 ```

 Nginx proxies the control site to port 8090 and serves the authenticated, unbuffered MP3 endpoint at:

 ```text
   https://radio.example.com/stream.mp3
 ```

 9. Start using it

 1. Open the configured site.
 2. Sign in with RADIO_USERNAME and RADIO_PASSWORD.
 3. Add one or more YouTube podcast sources.
 4. Wait for an episode to download and prepare.
 5. Select a Spotify playlist or Song Radio seed.
 6. Configure the number of songs per music break.
 7. Press START RADIO.
 8. Listen in the browser or open /stream.mp3 in VLC.

 For restricted YouTube content, place a Netscape-format cookie file at:

 ```text
   data/state/youtube-cookies.txt
 ```

 Keep cookies, Spotify OAuth credentials, generated media and .env private; none should be committed to Git.
