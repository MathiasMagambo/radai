 ### Podcasts

 1. The user adds a YouTube channel link or individual video link.
 2. yt-dlp retrieves metadata, subtitles and audio.
 3. The DeepSeek API analyzes the transcript and returns:
   - Advertisement ranges to remove
   - Topic boundaries suitable for music breaks
 4. FFmpeg removes selected ranges, normalizes the audio and produces prepared MP3 files.
 5. During playback, the engine decodes podcast segments to PCM.
 6. Podcast episode and position checkpoints are persisted, allowing playback to resume after a service restart.

 ### Spotify

 Spotify uses two cooperating applications:

 - Spotify Desktop for Linux (https://snapcraft.io/spotify) provides the actual Spotify interface and Song Radio workflow.
 - spotifyd (https://github.com/Spotifyd/spotifyd) provides the Radai Radio Spotify Connect output device.

 Radai controls Spotify Desktop through its Chromium DevTools interface using websocket-client. It searches tracks, starts Song Radio, transfers playback
 to Radai Radio, resumes existing contexts and counts completed songs.

 The engine launches spotifyd itself with a generated pipe-backend configuration. Do not run a second independent spotifyd service.
 
 ### Radio and listening

 - Start/stop the station: START RADIO and STOP control the shared server-side broadcast.
 - Listen in the browser: PLAY only starts the live stream on your current device; it does not start
 the station itself.
 - Volume controls: Use VOL to mute/unmute and the adjacent slider to adjust browser volume.
 - Resume support: Podcasts retain their playback checkpoint across pauses and service restarts.
 - Private access: The control site and stream require authentication.

 ### Podcasts

 - YouTube channel sources: Under Podcast Sources → Add Sources, paste a YouTube channel URL. Radai
 discovers and prepares recent unplayed episodes.
 - Individual videos: Paste a YouTube video URL and select PREPARE. Once ready, choose QUEUE IT or
 PLAY NOW.
 - Prepared podcast chooser: Enable Podcast chooser in Settings. Use the arrows beneath the current
 title to select what plays next, or press PLAY NOW to switch immediately.
 - Podcast history: Open History to replay prepared episodes or prepare older episodes again.
 - Source management: Open Sources to search or remove configured YouTube channels.
 - AI processing: Radai uses podcast transcripts to detect advertising and identify suitable topic
 boundaries for music breaks.
 - Ad removal: Processed podcast audio can have detected advertising removed before broadcast.
 - Storage controls: Settings determine how many unplayed episodes are prepared and how many played
 episodes remain stored per source.

### Spotify music

 - Playlist source: Choose a saved Spotify playlist under Music Source, then confirm with SWITCH
 SOURCE.
 - Random playlist mode: Clear the current source with the × button to rotate through random saved
 Spotify playlists.
 - Song radio: Search for a song or artist, then select START RADIO or SWAP SOURCE beside a result.
 Radai builds the music source around that track.
 - Automatic music breaks: Radai inserts a configurable number of Spotify songs between podcast
 sections.
 - Break placement: In Settings, breaks can replace detected ad sections or use AI-selected topic
 boundaries.
 - Songs per break: Configurable from 1–10 songs.

 Podcast timeline and seeking

 - The timeline displays the current podcast position, total duration, progress, and yellow
 music-break markers.
 - Shared seeking: Drag the normal timeline scrubber to seek the server-side podcast. The change
 applies to every listener and may take roughly 15 seconds to reach the buffered stream.
 - Add a music break:
   1. Select ADD MUSIC BREAK.
   2. The timeline turns white and the scrubber becomes a +.
   3. Place it at a future position.
   4. The new yellow marker appears immediately and is saved permanently.
 - Breaks cannot be inserted behind the current live position.
 - Adding a break does not pause or seek the podcast.
 - Restart podcast: Enable Restart current podcast in Settings to show a restart button beside the
 current title.

 ### Playback modes

 The Pure radio setting controls what happens when a listener pauses:

 - On: Pausing the browser player only stops playback on that device; the shared station continues
 broadcasting.
 - Off: Pausing also pauses the shared podcast or music. Playback resumes from the same position
 later.

 ### Reliability

 - Radai, Spotify Desktop, and the virtual display run as persistent systemd services.
 - The station remains available after SSH logout.
 - Paused playback remains paused after the SSH session closes.
 - Podcast checkpoints, configured sources, prepared episodes, history, settings, and manually added
 breaks are persisted on the server.
