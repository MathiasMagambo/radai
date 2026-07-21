const $ = (selector) => document.querySelector(selector);
const stateText = $('#stateText');
const stateDot = $('#stateDot');
const nowPlaying = $('#nowPlaying');
const detail = $('#detail');
const podcast = $('#podcast');
const message = $('#message');
const preparationError = $('#preparationError');
const restartPodcast = $('#restartPodcast');
const startButton = $('#startButton');
const stopButton = $('#stopButton');
const player = $('#player');
const streamPlay = $('#streamPlay');
const streamMute = $('#streamMute');
const streamVolume = $('#streamVolume');
const streamStatus = $('#streamStatus');
const musicBreakLink = $('#musicBreakLink');
const podcastTimeline = $('#podcastTimeline');
const podcastPosition = $('#podcastPosition');
const podcastDuration = $('#podcastDuration');
const podcastBreaks = $('#podcastBreaks');
const podcastProgress = $('#podcastProgress');
const musicBreakCursor = $('#musicBreakCursor');
const podcastSeekLimit = $('#podcastSeekLimit');
const playlistInput = $('#playlist');
const playlistOptions = $('#playlistOptions');
const activePlaylistName = $('#activePlaylistName');
const activePlaylistLabel = $('#activePlaylistLabel');
const activePlaylistClear = $('#activePlaylistClear');
const playlistClear = $('#playlistClear');
const playlistConfirm = $('#playlistConfirm');
const playlistConfirmText = $('#playlistConfirmText');
const settingsButton = $('#settingsButton');
const settingsOverlay = $('#settingsOverlay');
const settingsControls = Array.from(settingsOverlay.querySelectorAll('input, select'));
const podcastSelector = $('#podcastSelector');
const podcastChoiceTitle = $('#podcastChoiceTitle');
const podcastChoiceChannel = $('#podcastChoiceChannel');
const podcastPrevious = $('#podcastPrevious');
const podcastNext = $('#podcastNext');
const podcastPlayNow = $('#podcastPlayNow');
const videoDecision = $('#videoDecision');
const videoDecisionTitle = $('#videoDecisionTitle');
const sourceSearch = $('#sourceSearch');
const podcastHistoryList = $('#podcastHistory');

let radioSettings = {
  playback_mode: 'resumable',
  music_placement: 'ads',
  songs_per_break: 3,
  restart_current_podcast_enabled: false,
  podcast_selector_enabled: false,
  unplayed_episodes_per_source: 1,
  played_episodes_per_source: 1,
  stream_delay_sec: 15,
  podcast_seek_step_sec: 1,
  music_break_future_guard_sec: 18,
};
let lastStatus = { state: 'stopped', mode: 'idle' };
let playlistCatalog = [];
let proposedPlaylist = null;
let playlistChoiceIndex = -1;
let preparedPodcasts = [];
let podcastChoiceIndex = -1;
let podcastChoicePending = false;
let podcastHistory = [];
let streamConnected = false;
let streamWanted = false;
let streamReconnectTimer = null;
let streamReconnecting = false;
let pendingReplay = null;
let timelineScrubbing = false;
let timelineBreakKey = '';
let musicBreakSelecting = false;
let musicBreakSubmitting = false;
let musicBreakSelectionPosition = null;
let timelineAction = 'seek';
let musicBreakSelectionEpisodeId = null;
let pendingMusicBreak = null;
let lastMusicBreakError = '';
let lastPreparationWarning = '';
let channelSources = [];
let settingsDirty = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (response.status === 401) {
    window.location.replace('/login');
    throw new Error('Sign in required');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function notify(text, error = false) {
  message.textContent = text;
  message.style.color = error ? '#ff7676' : '';
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { message.textContent = ''; }, 5000);
}

function isActive(status = lastStatus) {
  return ['running', 'starting', 'paused'].includes(status.state);
}

function updateStreamControl(status) {
  const playingOrConnecting = streamWanted;
  streamPlay.textContent = playingOrConnecting ? 'PAUSE' : 'PLAY';
  streamPlay.setAttribute('aria-label', `${playingOrConnecting ? 'Pause' : 'Play'} radio stream`);
  streamPlay.setAttribute('aria-pressed', String(playingOrConnecting));
  streamPlay.classList.toggle('playing', playingOrConnecting);
  streamMute.textContent = player.muted || player.volume === 0 ? 'MUTE' : 'VOL';
  streamMute.setAttribute('aria-pressed', String(player.muted));
  streamStatus.textContent = status || (!player.paused ? 'LIVE' : (streamWanted ? 'CONNECTING' : 'READY'));
}

function cancelStreamReconnect() {
  window.clearTimeout(streamReconnectTimer);
  streamReconnectTimer = null;
}

function scheduleStreamReconnect(delay = 2500) {
  if (!streamWanted || pendingReplay || timelineScrubbing || streamReconnectTimer !== null) return;
  streamReconnectTimer = window.setTimeout(() => {
    streamReconnectTimer = null;
    reconnectStream();
  }, delay);
}

function requestPlayerPlayback({ reload = false } = {}) {
  if (reload) {
    streamConnected = false;
    player.load();
  }
  scheduleStreamReconnect(5000);
  return player.play();
}

async function reconnectStream() {
  if (!streamWanted || pendingReplay || timelineScrubbing || streamReconnecting) return;
  streamReconnecting = true;
  updateStreamControl('CONNECTING');
  try {
    const data = await api('/api/status');
    let status = data.status;
    if (!isActive(status)) {
      updateStreamControl('BUFFERING');
      scheduleStreamReconnect(3000);
      return;
    }
    if (status.state === 'paused' && radioSettings.playback_mode === 'resumable') {
      const resumed = await api('/api/resume', { method: 'POST', body: '{}' });
      status = resumed.status;
    }
    renderStatus(status);
    requestPlayerPlayback({ reload: true }).catch(() => {
      updateStreamControl('BUFFERING');
      scheduleStreamReconnect(3000);
    });
  } catch (_error) {
    updateStreamControl('BUFFERING');
    scheduleStreamReconnect(3000);
  } finally {
    streamReconnecting = false;
  }
}

async function pausePlayback() {
  if (pendingReplay) pendingReplay.resumeWanted = false;
  streamWanted = false;
  cancelStreamReconnect();
  player.pause();
  updateStreamControl('PAUSED');
  if (radioSettings.playback_mode === 'resumable' && isActive()) {
    const data = await api('/api/pause', { method: 'POST', body: '{}' });
    renderStatus(data.status);
  }
}

async function playPlayback() {
  if (pendingReplay) {
    streamWanted = true;
    pendingReplay.resumeWanted = true;
    updateStreamControl('PREPARING');
    return;
  }
  streamWanted = true;
  updateStreamControl('CONNECTING');
  const current = await api('/api/status');
  let status = current.status;
  if (!isActive(status)) {
    const started = await api('/api/start', { method: 'POST', body: '{}' });
    status = started.status;
    streamConnected = false;
  } else if (status.state === 'paused' && radioSettings.playback_mode === 'resumable') {
    const resumed = await api('/api/resume', { method: 'POST', body: '{}' });
    status = resumed.status;
  }
  renderStatus(status);
  const reload = radioSettings.playback_mode === 'radio' || !streamConnected;
  await requestPlayerPlayback({ reload });
}

streamPlay.addEventListener('click', async () => {
  try {
    if (streamWanted) await pausePlayback();
    else await playPlayback();
  } catch (error) {
    updateStreamControl(streamWanted ? 'UNAVAILABLE' : 'PAUSED');
    notify(error.message, true);
  }
});

streamMute.addEventListener('click', () => {
  player.muted = !player.muted;
  updateStreamControl();
});

streamVolume.addEventListener('input', () => {
  player.volume = Number(streamVolume.value);
  player.muted = false;
  updateStreamControl();
});

player.addEventListener('playing', () => {
  streamConnected = true;
  cancelStreamReconnect();
  updateStreamControl('LIVE');
});
player.addEventListener('waiting', () => {
  updateStreamControl('BUFFERING');
  scheduleStreamReconnect();
});
player.addEventListener('stalled', () => {
  updateStreamControl('BUFFERING');
  scheduleStreamReconnect();
});
player.addEventListener('ended', () => {
  streamConnected = false;
  updateStreamControl('BUFFERING');
  scheduleStreamReconnect(250);
});
player.addEventListener('pause', () => {
  updateStreamControl(pendingReplay ? 'PREPARING' : (streamWanted ? 'BUFFERING' : 'PAUSED'));
  if (streamWanted) scheduleStreamReconnect(500);
});
player.addEventListener('error', () => {
  streamConnected = false;
  updateStreamControl('STREAM ERROR');
  scheduleStreamReconnect();
});

function formatPlaybackTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
    : `${minutes}:${String(remainder).padStart(2, '0')}`;
}

function musicBreakMinimum(status) {
  const position = Math.max(0, Number(status.podcast_position_sec) || 0);
  const guard = Math.max(1, Number(radioSettings.music_break_future_guard_sec) || 18);
  return Math.ceil(position + guard);
}

function withPendingMusicBreak(status) {
  if (!pendingMusicBreak || status.podcast_id !== pendingMusicBreak.podcastId) return status;
  const breaks = Array.isArray(status.music_breaks_sec) ? status.music_breaks_sec : [];
  if (breaks.some(position => Math.abs(Number(position) - pendingMusicBreak.position) < 1)) {
    return status;
  }
  return {
    ...status,
    music_breaks_sec: [...breaks, pendingMusicBreak.position].sort((a, b) => a - b),
  };
}

function updateMusicBreakCursor(duration) {
  const position = Number(musicBreakSelectionPosition);
  if (!(duration > 0) || !Number.isFinite(position)) return;
  musicBreakCursor.style.left = `${Math.min(100, Math.max(0, (position / duration) * 100))}%`;
}

function renderMusicBreakControl(status) {
  const duration = Number(status.podcast_duration_sec);
  const available = duration > 0 && ['running', 'paused'].includes(status.state);
  const busy = musicBreakSubmitting || Boolean(status.music_break_pending);
  if (
    musicBreakSelecting
    && (
      !available
      || !status.podcast_id
      || status.podcast_id !== musicBreakSelectionEpisodeId
    )
  ) {
    musicBreakSelecting = false;
    timelineScrubbing = false;
    musicBreakSelectionPosition = null;
  }
  const active = musicBreakSelecting || busy;
  podcastProgress.parentElement.classList.toggle('adding-break', active);
  musicBreakLink.classList.toggle('selecting', musicBreakSelecting);
  musicBreakLink.disabled = !available || busy;
  musicBreakLink.textContent = busy
    ? 'ADDING MUSIC BREAK…'
    : (musicBreakSelecting ? 'CANCEL MUSIC BREAK' : 'ADD MUSIC BREAK');
  podcastProgress.disabled = busy;
  updateMusicBreakCursor(duration);
  const error = status.music_break_error || '';
  if (error && error !== lastMusicBreakError) notify(error, true);
  lastMusicBreakError = error;
}

function renderPodcastTimeline(status) {
  const duration = Number(status.podcast_duration_sec);
  if (!(duration > 0)) {
    podcastTimeline.hidden = true;
    return;
  }
  const position = Math.min(duration, Math.max(0, Number(status.podcast_position_sec) || 0));
  podcastTimeline.hidden = false;
  podcastProgress.max = String(duration);
  podcastProgress.step = String(radioSettings.podcast_seek_step_sec || 1);
  if (musicBreakSelecting) {
    const minimum = musicBreakMinimum(status);
    podcastProgress.min = String(minimum);
    musicBreakSelectionPosition = Math.max(
      minimum,
      Number(musicBreakSelectionPosition) || minimum,
    );
    podcastProgress.value = String(musicBreakSelectionPosition);
    podcastPosition.textContent = formatPlaybackTime(musicBreakSelectionPosition);
  } else if (!timelineScrubbing) {
    podcastProgress.min = '0';
    podcastProgress.value = String(position);
    podcastPosition.textContent = formatPlaybackTime(position);
  }
  podcastDuration.textContent = formatPlaybackTime(duration);
  podcastProgress.setAttribute(
    'aria-valuetext',
    `${formatPlaybackTime(podcastProgress.value)} of ${formatPlaybackTime(duration)}`,
  );
  podcastProgress.parentElement.style.setProperty(
    '--podcast-progress',
    `${duration ? (Number(podcastProgress.value) / duration) * 100 : 0}%`,
  );
  updateMusicBreakCursor(duration);
  const breaks = Array.isArray(status.music_breaks_sec) ? status.music_breaks_sec : [];
  const breakKey = `${status.podcast_id}:${duration}:${breaks.join(',')}`;
  if (breakKey !== timelineBreakKey) {
    timelineBreakKey = breakKey;
    podcastBreaks.replaceChildren(...breaks.map((breakPosition) => {
      const marker = document.createElement('span');
      marker.className = 'podcast-break';
      marker.style.left = `${Math.min(100, Math.max(0, (Number(breakPosition) / duration) * 100))}%`;
      marker.title = `Music break at ${formatPlaybackTime(breakPosition)}`;
      return marker;
    }));
  }
  const delay = Math.max(0, Math.round(Number(radioSettings.stream_delay_sec) || 0));
  podcastSeekLimit.textContent = musicBreakSelecting
    ? `SELECT A FUTURE BREAK · ${formatPlaybackTime(Number(podcastProgress.min))} MINIMUM`
    : `SHARED SEEK · FULL EPISODE · ${podcastProgress.step} SEC STEPS · ~${delay} SEC APPLY`;
}

function renderStatus(status) {
  let resumeReplay = false;
  if (pendingReplay) {
    if (status.state === 'error') {
      pendingReplay = null;
      renderPodcastHistory();
    } else {
      if (status.state === 'starting' || status.mode === 'preparing') {
        pendingReplay.observed = true;
      }
      const replayReady = (
        status.state === 'running'
        && status.mode === 'podcast'
        && status.now_playing === pendingReplay.title
        && (pendingReplay.observed || status.now_playing !== pendingReplay.previousTitle)
      );
      if (replayReady) {
        resumeReplay = pendingReplay.resumeWanted && streamWanted;
        pendingReplay = null;
        renderPodcastHistory();
      } else {
        status = {
          ...status,
          state: 'starting',
          detail: `Preparing ${pendingReplay.title}`,
          mode: 'preparing',
          now_playing: '',
          podcast: pendingReplay.title,
          error: null,
        };
      }
    }
  }
  status = withPendingMusicBreak(status);
  lastStatus = status;
  stateText.textContent = status.state.toUpperCase();
  stateDot.classList.toggle('live', status.state === 'running');
  stateDot.classList.toggle('paused', status.state === 'paused');
  nowPlaying.textContent = status.now_playing || (status.mode === 'preparing' ? 'PREPARING PODCAST' : '—');
  detail.textContent = status.error || status.detail;
  podcast.textContent = status.podcast && status.podcast !== status.now_playing ? `PODCAST: ${status.podcast}` : '';
  preparationError.hidden = !status.preparation_error;
  preparationError.textContent = status.preparation_error || '';
  const warning = status.preparation_warning || '';
  if (warning && warning !== lastPreparationWarning) notify(warning);
  if (warning) lastPreparationWarning = warning;
  startButton.disabled = isActive(status);
  stopButton.disabled = !isActive(status) && status.state !== 'error';
  restartPodcast.hidden = !(
    radioSettings.restart_current_podcast_enabled
    && status.podcast
    && isActive(status)
  );
  for (const button of document.querySelectorAll('.song-radio-button')) {
    button.textContent = isActive(status) ? 'SWAP SOURCE' : 'START RADIO';
  }
  renderPodcastTimeline(status);
  renderMusicBreakControl(status);
  if (resumeReplay) {
    updateStreamControl('CONNECTING');
    requestPlayerPlayback({ reload: true }).catch(() => {
      updateStreamControl('BUFFERING');
      scheduleStreamReconnect(3000);
    });
  }
}

async function refreshStatus() {
  try {
    const data = await api('/api/status');
    renderStatus(data.status);
  } catch (error) {
    stateText.textContent = 'OFFLINE';
    stateDot.classList.remove('live', 'paused');
    detail.textContent = error.message;
  }
}

function prepareTimelineSeek() {
  if (!musicBreakSelecting && !musicBreakSubmitting) timelineAction = 'seek';
}

podcastProgress.addEventListener('pointerdown', prepareTimelineSeek);
podcastProgress.addEventListener('keydown', prepareTimelineSeek);

podcastProgress.addEventListener('input', () => {
  timelineScrubbing = true;
  const duration = Number(podcastProgress.max) || 1;
  const position = Number(podcastProgress.value) || 0;
  if (timelineAction === 'music-break') musicBreakSelectionPosition = position;
  podcastPosition.textContent = formatPlaybackTime(position);
  podcastProgress.setAttribute(
    'aria-valuetext',
    `${formatPlaybackTime(position)} of ${formatPlaybackTime(duration)}`,
  );
  podcastProgress.parentElement.style.setProperty(
    '--podcast-progress',
    `${(position / duration) * 100}%`,
  );
  updateMusicBreakCursor(duration);
});

podcastProgress.addEventListener('change', async () => {
  if (timelineAction === 'music-break') {
    if (
      !musicBreakSelecting
      || !lastStatus.podcast_id
      || lastStatus.podcast_id !== musicBreakSelectionEpisodeId
    ) {
      timelineAction = 'seek';
      timelineScrubbing = false;
      renderStatus(lastStatus);
      return;
    }
    const position = Number(podcastProgress.value) || 0;
    const minimum = musicBreakMinimum(lastStatus);
    if (position < minimum) {
      notify('MUSIC BREAK MUST BE AHEAD OF THE LIVE PODCAST POSITION', true);
      renderStatus(lastStatus);
      return;
    }
    timelineAction = 'seek';
    musicBreakSelectionEpisodeId = null;
    musicBreakSelecting = false;
    musicBreakSubmitting = true;
    pendingMusicBreak = {
      podcastId: lastStatus.podcast_id,
      position,
    };
    musicBreakSelectionPosition = position;
    renderStatus(lastStatus);
    try {
      const data = await api('/api/music-break', {
        method: 'POST',
        body: JSON.stringify({ position_sec: position }),
      });
      renderStatus(data.status);
      notify(`MUSIC BREAK SCHEDULED AT ${formatPlaybackTime(position)}`);
    } catch (error) {
      pendingMusicBreak = null;
      notify(error.message, true);
      await refreshStatus();
    } finally {
      pendingMusicBreak = null;
      musicBreakSubmitting = false;
      timelineScrubbing = false;
      renderStatus(lastStatus);
    }
    return;
  }
  timelineScrubbing = true;
  const position = Number(podcastProgress.value) || 0;
  const resumeAfterSeek = streamWanted;
  podcastProgress.disabled = true;
  cancelStreamReconnect();
  player.pause();
  streamConnected = false;
  updateStreamControl('SEEKING');
  try {
    const data = await api('/api/seek', {
      method: 'POST',
      body: JSON.stringify({ position_sec: position }),
    });
    renderStatus(data.status);
    timelineScrubbing = false;
    if (resumeAfterSeek) {
      await requestPlayerPlayback({ reload: true });
    }
    notify(`SEEKING TO ${formatPlaybackTime(position)}`);
  } catch (error) {
    notify(error.message, true);
    await refreshStatus();
  } finally {
    timelineScrubbing = false;
    podcastProgress.disabled = false;
  }
});

musicBreakLink.addEventListener('click', () => {
  if (musicBreakSelecting) {
    musicBreakSelecting = false;
    timelineScrubbing = false;
    musicBreakSelectionPosition = null;
    timelineAction = 'seek';
    musicBreakSelectionEpisodeId = null;
    renderStatus(lastStatus);
    return;
  }
  const duration = Number(lastStatus.podcast_duration_sec);
  const minimum = musicBreakMinimum(lastStatus);
  if (!(duration > 0) || minimum >= duration - 1) {
    notify('NO FUTURE PODCAST POSITION IS AVAILABLE FOR A MUSIC BREAK', true);
    return;
  }
  timelineAction = 'music-break';
  musicBreakSelectionEpisodeId = lastStatus.podcast_id;
  musicBreakSelecting = true;
  timelineScrubbing = true;
  musicBreakSelectionPosition = minimum;
  renderStatus(lastStatus);
  podcastProgress.focus();
});

startButton.addEventListener('click', async () => {
  startButton.disabled = true;
  try {
    const data = await api('/api/start', { method: 'POST', body: '{}' });
    renderStatus(data.status);
    streamWanted = true;
    await requestPlayerPlayback({ reload: true });
    notify('RADIO STARTED');
  } catch (error) { notify(error.message, true); }
});

stopButton.addEventListener('click', async () => {
  stopButton.disabled = true;
  try {
    const data = await api('/api/stop', { method: 'POST', body: '{}' });
    renderStatus(data.status);
    streamWanted = false;
    cancelStreamReconnect();
    player.pause();
    streamConnected = false;
    notify('RADIO STOPPED');
  } catch (error) { notify(error.message, true); }
});
restartPodcast.addEventListener('click', async () => {
  restartPodcast.disabled = true;
  try {
    const data = await api('/api/restart-podcast', { method: 'POST', body: '{}' });
    renderStatus(data.status);
    notify('RESTARTING PODCAST');
  } catch (error) {
    notify(error.message, true);
  } finally {
    restartPodcast.disabled = false;
  }
});

function renderSettings(settings) {
  radioSettings = { ...radioSettings, ...settings };
  if (!settingsDirty) {
    $('#pureRadio').checked = radioSettings.playback_mode === 'radio';
    $('#podcastSelectorEnabled').checked = radioSettings.podcast_selector_enabled;
    $('#restartPodcastEnabled').checked = radioSettings.restart_current_podcast_enabled;
    $('#musicPlacement').value = radioSettings.music_placement;
    $('#songsPerBreak').value = radioSettings.songs_per_break;
    $('#unplayedEpisodesPerSource').value = radioSettings.unplayed_episodes_per_source;
    $('#playedEpisodesPerSource').value = radioSettings.played_episodes_per_source;
  }
  const selectedName = radioSettings.selected_playlist_name || '';
  syncPlaylistClearButton();
  const configuredSourceName = radioSettings.seed_track_name || selectedName;
  const configuredSourceUri = radioSettings.seed_track_uri || radioSettings.selected_playlist_uri;
  const activeSourceName = radioSettings.active_music_source_name || '';
  const usesRandomSource = !configuredSourceUri;
  const sourceIsPending = configuredSourceName
    && configuredSourceUri !== radioSettings.active_music_source_uri;
  activePlaylistLabel.textContent = sourceIsPending ? 'SELECTED MUSIC SOURCE' : 'CURRENT MUSIC SOURCE';
  activePlaylistName.textContent = usesRandomSource
    ? 'Random saved playlist'
    : (sourceIsPending ? configuredSourceName : (activeSourceName || configuredSourceName));
  syncActiveSourceClearButton();
  $('#queuedVideo').textContent = radioSettings.queued_video_title
    ? `PLAY NEXT: ${radioSettings.queued_video_title}`
    : '';
  videoDecision.hidden = !radioSettings.pending_video_id;
  videoDecisionTitle.textContent = radioSettings.pending_video_title || '';
  restartPodcast.hidden = !(
    radioSettings.restart_current_podcast_enabled
    && lastStatus.podcast
    && isActive(lastStatus)
  );
  renderPodcastSelector();
}

function renderPodcastSelector() {
  podcastSelector.hidden = !radioSettings.podcast_selector_enabled;
  if (podcastSelector.hidden) return;
  const selected = preparedPodcasts[podcastChoiceIndex];
  if (selected) {
    podcastChoiceTitle.textContent = selected.title;
    podcastChoiceChannel.textContent = selected.channel;
  } else if (radioSettings.queued_video_title) {
    podcastChoiceTitle.textContent = radioSettings.queued_video_title;
    podcastChoiceChannel.textContent = 'Queued YouTube video';
  } else {
    podcastChoiceTitle.textContent = 'No prepared podcast';
    podcastChoiceChannel.textContent = 'Waiting for preparation';
  }
  const available = preparedPodcasts.length > 0 && !podcastChoicePending;
  podcastPrevious.disabled = !available;
  podcastNext.disabled = !available;
  podcastPlayNow.disabled = !available;
}

async function loadPodcasts() {
  const data = await api('/api/podcasts');
  preparedPodcasts = data.podcasts;
  podcastChoiceIndex = preparedPodcasts.findIndex((item) => item.queued);
  if (podcastChoiceIndex < 0 && !radioSettings.queued_video_title && preparedPodcasts.length) {
    podcastChoiceIndex = 0;
  }
  renderPodcastSelector();
}

async function choosePodcast(direction) {
  if (!preparedPodcasts.length || podcastChoicePending) return;
  podcastChoicePending = true;
  renderPodcastSelector();
  try {
    if (podcastChoiceIndex < 0) {
      podcastChoiceIndex = direction < 0 ? preparedPodcasts.length - 1 : 0;
    } else {
      podcastChoiceIndex = (
        podcastChoiceIndex + direction + preparedPodcasts.length
      ) % preparedPodcasts.length;
    }
    const selected = preparedPodcasts[podcastChoiceIndex];
    const data = await api('/api/podcast', {
      method: 'POST',
      body: JSON.stringify({ id: selected.id, action: 'queue' }),
    });
    preparedPodcasts = preparedPodcasts.map((item) => ({
      ...item,
      queued: item.id === selected.id,
    }));
    renderSettings(data.settings);
    notify(`PLAY NEXT: ${selected.title}`);
  } catch (error) {
    notify(error.message, true);
    await loadPodcasts().catch(() => {});
  } finally {
    podcastChoicePending = false;
    renderPodcastSelector();
  }
}

async function playChosenPodcastNow() {
  const selected = preparedPodcasts[podcastChoiceIndex];
  if (!selected || podcastChoicePending) return;
  podcastChoicePending = true;
  renderPodcastSelector();
  try {
    const data = await api('/api/podcast', {
      method: 'POST',
      body: JSON.stringify({ id: selected.id, action: 'play_now' }),
    });
    renderSettings(data.settings);
    renderStatus(data.status);
    if (player.paused) await playPlayback();
    notify(`PLAYING NOW: ${selected.title}`);
  } catch (error) {
    notify(error.message, true);
  } finally {
    podcastChoicePending = false;
    renderPodcastSelector();
  }
}

podcastPrevious.addEventListener('click', () => choosePodcast(-1));
podcastNext.addEventListener('click', () => choosePodcast(1));
podcastPlayNow.addEventListener('click', playChosenPodcastNow);

async function loadSettings() {
  const data = await api('/api/settings');
  renderSettings(data.settings);
  if (radioSettings.podcast_selector_enabled) await loadPodcasts();
}

function animateSettings(direction) {
  settingsButton.classList.remove('roll-clockwise', 'roll-counterclockwise');
  void settingsButton.offsetWidth;
  settingsButton.classList.add(direction === 'open' ? 'roll-clockwise' : 'roll-counterclockwise');
}

settingsControls.forEach((control) => {
  control.addEventListener('input', () => { settingsDirty = true; });
  control.addEventListener('change', () => { settingsDirty = true; });
});

function openSettings() {
  settingsDirty = false;
  renderSettings(radioSettings);
  animateSettings('open');
  settingsOverlay.hidden = false;
  settingsButton.setAttribute('aria-expanded', 'true');
  settingsButton.setAttribute('aria-label', 'Close settings');
  $('#settingsClose').focus();
}

function closeSettings() {
  settingsDirty = false;
  animateSettings('close');
  settingsOverlay.classList.add('closing');
  settingsButton.setAttribute('aria-expanded', 'false');
  settingsButton.setAttribute('aria-label', 'Open settings');
  window.setTimeout(() => {
    settingsOverlay.hidden = true;
    settingsOverlay.classList.remove('closing');
    settingsButton.focus();
  }, 260);
}

settingsButton.addEventListener('click', () => {
  if (settingsOverlay.hidden) openSettings();
  else closeSettings();
});
$('#settingsClose').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', (event) => {
  if (event.target === settingsOverlay) closeSettings();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !settingsOverlay.hidden) closeSettings();
});

$('#saveSettings').addEventListener('click', async () => {
  const button = $('#saveSettings');
  button.disabled = true;
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        playback_mode: $('#pureRadio').checked ? 'radio' : 'resumable',
        music_placement: $('#musicPlacement').value,
        podcast_selector_enabled: $('#podcastSelectorEnabled').checked,
        songs_per_break: Number($('#songsPerBreak').value),
        restart_current_podcast_enabled: $('#restartPodcastEnabled').checked,
        unplayed_episodes_per_source: Number($('#unplayedEpisodesPerSource').value),
        played_episodes_per_source: Number($('#playedEpisodesPerSource').value),
      }),
    });
    settingsDirty = false;
    renderSettings(data.settings);
    if (radioSettings.podcast_selector_enabled) await loadPodcasts();
    closeSettings();
    notify('SETTINGS SAVED');
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});

async function loadPlaylists() {
  try {
    const data = await api('/api/playlists');
    playlistCatalog = data.playlists;
    renderPlaylistOptions();
  } catch (error) { notify(error.message, true); }
}

function hidePlaylistOptions() {
  playlistChoiceIndex = -1;
  playlistOptions.hidden = true;
  playlistInput.setAttribute('aria-expanded', 'false');
  playlistInput.removeAttribute('aria-activedescendant');
}

function selectPlaylistOption(item) {
  playlistInput.value = item.name;
  syncPlaylistClearButton();
  hidePlaylistOptions();
  proposePlaylistChange(item);
}

function renderPlaylistOptions() {
  const query = playlistInput.value.trim().toLocaleLowerCase();
  const matches = playlistCatalog.filter(
    (item) => item.name.toLocaleLowerCase().includes(query)
  );
  playlistChoiceIndex = -1;
  playlistOptions.replaceChildren(...matches.map((item, index) => {
    const option = document.createElement('button');
    option.id = `playlist-option-${index}`;
    option.className = 'playlist-option';
    option.type = 'button';
    option.role = 'option';
    option.dataset.uri = item.uri;
    option.textContent = item.name;
    option.setAttribute('aria-selected', 'false');
    option.addEventListener('click', () => selectPlaylistOption(item));
    return option;
  }));
  const open = document.activeElement === playlistInput && matches.length > 0;
  playlistOptions.hidden = !open;
  playlistInput.setAttribute('aria-expanded', String(open));
  playlistInput.removeAttribute('aria-activedescendant');
}

function movePlaylistChoice(direction) {
  const options = Array.from(playlistOptions.querySelectorAll('.playlist-option'));
  if (!options.length) return;
  playlistChoiceIndex = (
    playlistChoiceIndex + direction + options.length
  ) % options.length;
  options.forEach((option, index) => {
    option.setAttribute('aria-selected', String(index === playlistChoiceIndex));
  });
  const selected = options[playlistChoiceIndex];
  playlistInput.setAttribute('aria-activedescendant', selected.id);
  selected.scrollIntoView({ block: 'nearest' });
}

function syncPlaylistClearButton() {
  playlistClear.hidden = !playlistInput.value;
  playlistClear.setAttribute('aria-label', 'Clear playlist search');
}

function syncActiveSourceClearButton() {
  const hasConfiguredSource = Boolean(
    radioSettings.seed_track_uri || radioSettings.selected_playlist_uri
  );
  activePlaylistClear.hidden = !hasConfiguredSource || !playlistConfirm.hidden;
}

function hidePlaylistConfirmation() {
  proposedPlaylist = null;
  playlistConfirm.hidden = true;
  syncActiveSourceClearButton();
}

function proposePlaylistChange(selection = null) {
  const value = playlistInput.value.trim();
  const match = selection || playlistCatalog.find(
    (item) => item.name.toLowerCase() === value.toLowerCase()
  );
  if (value && !match) {
    hidePlaylistConfirmation();
    return;
  }
  const proposal = match || { uri: '', name: '' };
  if ((radioSettings.selected_playlist_uri || '') === proposal.uri) {
    hidePlaylistConfirmation();
    return;
  }
  proposedPlaylist = proposal;
  playlistConfirmText.textContent = `Switch to ${proposal.name || 'a random saved playlist'}?`;
  playlistConfirm.hidden = false;
  syncActiveSourceClearButton();
}

playlistInput.addEventListener('focus', renderPlaylistOptions);
playlistInput.addEventListener('input', () => {
  syncPlaylistClearButton();
  proposePlaylistChange();
  renderPlaylistOptions();
});
playlistInput.addEventListener('change', proposePlaylistChange);
playlistInput.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (playlistOptions.hidden) renderPlaylistOptions();
    movePlaylistChoice(event.key === 'ArrowDown' ? 1 : -1);
    return;
  }
  if (event.key === 'Enter' && playlistChoiceIndex >= 0) {
    event.preventDefault();
    const selected = playlistOptions.querySelectorAll('.playlist-option')[playlistChoiceIndex];
    const item = playlistCatalog.find((candidate) => candidate.uri === selected?.dataset.uri);
    if (item) selectPlaylistOption(item);
    return;
  }
  if (event.key === 'Escape') hidePlaylistOptions();
});
playlistInput.addEventListener('dblclick', () => {
  playlistInput.select();
  renderPlaylistOptions();
});
document.addEventListener('click', (event) => {
  if (!event.target.closest('.playlist-picker')) hidePlaylistOptions();
});
playlistClear.addEventListener('click', () => {
  playlistInput.value = '';
  hidePlaylistConfirmation();
  syncPlaylistClearButton();
  playlistInput.focus();
});
$('#playlistCancel').addEventListener('click', () => {
  playlistInput.value = '';
  syncPlaylistClearButton();
  hidePlaylistConfirmation();
});
$('#playlistApply').addEventListener('click', async () => {
  if (!proposedPlaylist) return;
  const button = $('#playlistApply');
  const selection = proposedPlaylist;
  button.disabled = true;
  try {
    const data = await api('/api/playlist', {
      method: 'POST',
      body: JSON.stringify({ uri: selection.uri, name: selection.name }),
    });
    playlistInput.value = '';
    hidePlaylistConfirmation();
    renderSettings(data.settings);
    notify(selection.name ? `PLAYLIST: ${selection.name}` : 'PLAYLIST: RANDOM');
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

activePlaylistClear.addEventListener('click', async () => {
  activePlaylistClear.disabled = true;
  try {
    const data = await api('/api/playlist', {
      method: 'POST',
      body: JSON.stringify({ uri: '', name: '' }),
    });
    playlistInput.value = '';
    hidePlaylistConfirmation();
    renderSettings(data.settings);
    notify('PLAYLIST: RANDOM');
  } catch (error) {
    notify(error.message, true);
  } finally {
    activePlaylistClear.disabled = false;
  }
});

$('#searchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = $('#search').value.trim();
  const results = $('#searchResults');
  results.textContent = 'SEARCHING…';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    results.replaceChildren(...data.tracks.map((track) => {
      const row = document.createElement('div');
      row.className = 'result';
      const copy = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = track.name;
      const meta = document.createElement('span');
      meta.textContent = `${track.artists.join(', ')} · ${track.album}`;
      copy.append(title, meta);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'song-radio-button';
      button.textContent = isActive() ? 'SWAP SOURCE' : 'START RADIO';
      button.addEventListener('click', async () => {
        try {
          const swapping = isActive();
          const result = await api('/api/radio-track', {
            method: 'POST',
            body: JSON.stringify({ uri: track.uri, name: `${track.name} — ${track.artists.join(', ')}` }),
          });
          if (result.settings) renderSettings(result.settings);
          await refreshStatus();
          notify(`${swapping ? 'MUSIC SOURCE SWAPPED' : 'SONG RADIO'}: ${track.name}`);
        } catch (error) { notify(error.message, true); }
      });
      row.append(copy, button);
      return row;
    }));
    if (!data.tracks.length) results.textContent = 'NO TRACKS FOUND';
  } catch (error) { results.textContent = ''; notify(error.message, true); }
});

function renderChannels() {
  const query = sourceSearch.value.trim().toLowerCase();
  const visible = channelSources.filter((source) => (
    !query
    || source.name.toLowerCase().includes(query)
    || source.url.toLowerCase().includes(query)
  ));
  const list = $('#channels');
  list.replaceChildren(...visible.map((source) => {
    const row = document.createElement('div');
    row.className = 'channel';
    const copy = document.createElement('div');
    copy.className = 'channel-copy';
    const name = document.createElement('strong');
    name.textContent = source.name;
    const url = document.createElement('span');
    url.textContent = source.url;
    copy.append(name, url);
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'REMOVE';
    button.addEventListener('click', async () => {
      try {
        await api('/api/channels/remove', {
          method: 'POST',
          body: JSON.stringify({ url: source.url }),
        });
        await loadChannels();
        notify('CHANNEL REMOVED');
      } catch (error) {
        notify(error.message, true);
      }
    });
    row.append(copy, button);
    return row;
  }));
  if (!visible.length) list.textContent = query ? 'NO MATCHING SOURCES' : 'NO SOURCES';
}

async function loadChannels() {
  const data = await api('/api/channels');
  channelSources = data.channels;
  renderChannels();
}

function renderPodcastHistory() {
  podcastHistoryList.replaceChildren(...podcastHistory.map((item) => {
    const row = document.createElement('div');
    row.className = 'channel history-item';
    const copy = document.createElement('div');
    copy.className = 'channel-copy';
    const title = document.createElement('strong');
    title.textContent = item.title;
    const channel = document.createElement('span');
    channel.textContent = item.channel;
    copy.append(title, channel);
    const button = document.createElement('button');
    const replaying = item.prepared && pendingReplay?.title === item.title;
    button.type = 'button';
    button.textContent = replaying
      ? 'PREPARING…'
      : (item.prepared ? 'PLAY AGAIN' : (item.preparing ? 'PREPARING…' : 'PREPARE'));
    button.disabled = item.preparing || replaying;
    button.classList.toggle('primary', item.prepared);
    if (replaying) button.setAttribute('aria-busy', 'true');
    button.addEventListener('click', async () => {
      const previousStatus = lastStatus;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      try {
        if (item.prepared) {
          pendingReplay = {
            title: item.title,
            previousTitle: lastStatus.now_playing || '',
            resumeWanted: streamWanted,
            observed: false,
          };
          cancelStreamReconnect();
          player.pause();
          streamConnected = false;
          updateStreamControl('PREPARING');
          button.textContent = 'PREPARING…';
          renderStatus(lastStatus);
          notify(`PREPARING: ${item.title}`);
          const data = await api('/api/history', {
            method: 'POST',
            body: JSON.stringify({ id: item.id, action: 'play_again' }),
          });
          renderSettings(data.settings);
          await refreshStatus();
          notify(`SWITCHING PODCAST: ${item.title}`);
        } else {
          await api('/api/history', {
            method: 'POST',
            body: JSON.stringify({ id: item.id, action: 'prepare' }),
          });
          item.preparing = true;
          button.textContent = 'PREPARING…';
          notify(`PREPARING: ${item.title}`);
        }
      } catch (error) {
        item.preparing = false;
        const resumeAfterFailure = pendingReplay?.resumeWanted && streamWanted;
        pendingReplay = null;
        if (item.prepared) renderStatus(previousStatus);
        if (resumeAfterFailure) requestPlayerPlayback({ reload: true }).catch(() => {});
        notify(error.message, true);
      } finally {
        const replaying = item.prepared && pendingReplay?.title === item.title;
        if (!replaying) button.removeAttribute('aria-busy');
        button.disabled = item.preparing || replaying;
        if (item.prepared) button.textContent = replaying ? 'PREPARING…' : 'PLAY AGAIN';
        else if (!item.preparing) button.textContent = 'PREPARE';
      }
    });
    row.append(copy, button);
    return row;
  }));
  if (!podcastHistory.length) podcastHistoryList.textContent = 'NO PLAYED PODCASTS';
}

async function loadPodcastHistory() {
  const data = await api('/api/history');
  podcastHistory = data.history;
  renderPodcastHistory();
}

for (const button of document.querySelectorAll('[data-source-page]')) {
  button.addEventListener('click', () => {
    const page = button.dataset.sourcePage;
    for (const tab of document.querySelectorAll('[data-source-page]')) {
      tab.classList.toggle('active', tab === button);
    }
    for (const panel of document.querySelectorAll('[data-source-panel]')) {
      panel.hidden = panel.dataset.sourcePanel !== page;
    }
    if (page === 'list') sourceSearch.focus();
    if (page === 'history') loadPodcastHistory().catch((error) => notify(error.message, true));
  });
}

sourceSearch.addEventListener('input', renderChannels);

$('#channelForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#channelUrl');
  try {
    await api('/api/channels', {
      method: 'POST',
      body: JSON.stringify({ url: input.value.trim() }),
    });
    input.value = '';
    await loadChannels();
    notify('CHANNEL ADDED');
  } catch (error) {
    notify(error.message, true);
  }
});

$('#videoForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#videoUrl');
  try {
    await api('/api/video', {
      method: 'POST',
      body: JSON.stringify({ url: input.value.trim() }),
    });
    input.value = '';
    $('#queuedVideo').textContent = 'PREPARING VIDEO…';
    notify('PREPARING YOUTUBE VIDEO');
  } catch (error) {
    notify(error.message, true);
  }
});

async function resolvePreparedVideo(action) {
  try {
    const data = await api('/api/video/action', {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
    renderSettings(data.settings);
    if (action === 'play_now') await refreshStatus();
    notify(action === 'play_now' ? 'SWITCHING PODCAST' : 'VIDEO QUEUED');
  } catch (error) {
    notify(error.message, true);
  }
}

$('#queueVideo').addEventListener('click', () => resolvePreparedVideo('queue'));
$('#playVideoNow').addEventListener('click', () => resolvePreparedVideo('play_now'));

updateStreamControl();
Promise.all([
  refreshStatus(),
  loadSettings(),
  loadPlaylists(),
  loadChannels(),
  loadPodcastHistory(),
])
  .then(() => renderSettings(radioSettings))
  .catch((error) => notify(error.message, true));
window.setInterval(refreshStatus, 3000);
window.setInterval(() => {
  loadSettings().catch((error) => notify(error.message, true));
  loadPodcastHistory().catch((error) => notify(error.message, true));
}, 5000);
