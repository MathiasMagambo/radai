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
const playlistInput = $('#playlist');
const playlistOptions = $('#playlistOptions');
const activePlaylistName = $('#activePlaylistName');
const playlistConfirm = $('#playlistConfirm');
const playlistConfirmText = $('#playlistConfirmText');
const settingsButton = $('#settingsButton');
const settingsOverlay = $('#settingsOverlay');
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
};
let lastStatus = { state: 'stopped', mode: 'idle' };
let playlistCatalog = [];
let proposedPlaylist = null;
let preparedPodcasts = [];
let podcastChoiceIndex = -1;
let podcastChoicePending = false;
let podcastHistory = [];
let streamConnected = false;
let streamWanted = false;
let streamReconnectTimer = null;
let streamReconnecting = false;
let channelSources = [];

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
  const playing = !player.paused;
  streamPlay.textContent = playing ? 'PAUSE' : 'PLAY';
  streamPlay.setAttribute('aria-label', `${playing ? 'Pause' : 'Play'} radio stream`);
  streamPlay.setAttribute('aria-pressed', String(playing));
  streamPlay.classList.toggle('playing', playing);
  streamMute.textContent = player.muted || player.volume === 0 ? 'MUTE' : 'VOL';
  streamMute.setAttribute('aria-pressed', String(player.muted));
  streamStatus.textContent = status || (playing ? 'LIVE' : 'READY');
}

function cancelStreamReconnect() {
  window.clearTimeout(streamReconnectTimer);
  streamReconnectTimer = null;
}

function scheduleStreamReconnect(delay = 2500) {
  if (!streamWanted || streamReconnectTimer !== null) return;
  streamReconnectTimer = window.setTimeout(() => {
    streamReconnectTimer = null;
    reconnectStream();
  }, delay);
}

async function reconnectStream() {
  if (!streamWanted || streamReconnecting) return;
  streamReconnecting = true;
  updateStreamControl('CONNECTING');
  try {
    const data = await api('/api/status');
    renderStatus(data.status);
    if (!isActive(data.status)) {
      updateStreamControl('BUFFERING');
      scheduleStreamReconnect(3000);
      return;
    }
    streamConnected = false;
    player.load();
    await player.play();
  } catch (_error) {
    updateStreamControl('BUFFERING');
    scheduleStreamReconnect(3000);
  } finally {
    streamReconnecting = false;
  }
}

async function pausePlayback() {
  streamWanted = false;
  cancelStreamReconnect();
  if (radioSettings.playback_mode === 'resumable' && isActive()) {
    const data = await api('/api/pause', { method: 'POST', body: '{}' });
    renderStatus(data.status);
  }
  player.pause();
}

async function playPlayback() {
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
  if (radioSettings.playback_mode === 'radio' || !streamConnected) player.load();
  await player.play();
}

streamPlay.addEventListener('click', async () => {
  try {
    if (!player.paused) await pausePlayback();
    else await playPlayback();
  } catch (error) {
    updateStreamControl('UNAVAILABLE');
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
  updateStreamControl(streamWanted ? 'BUFFERING' : 'PAUSED');
  if (streamWanted) scheduleStreamReconnect(500);
});
player.addEventListener('error', () => {
  streamConnected = false;
  updateStreamControl('STREAM ERROR');
  scheduleStreamReconnect();
});

function renderStatus(status) {
  lastStatus = status;
  stateText.textContent = status.state.toUpperCase();
  stateDot.classList.toggle('live', status.state === 'running');
  stateDot.classList.toggle('paused', status.state === 'paused');
  nowPlaying.textContent = status.now_playing || (status.mode === 'preparing' ? 'PREPARING PODCAST' : '—');
  detail.textContent = status.error || status.detail;
  podcast.textContent = status.podcast && status.podcast !== status.now_playing ? `PODCAST: ${status.podcast}` : '';
  preparationError.hidden = !status.preparation_error;
  preparationError.textContent = status.preparation_error || '';
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

startButton.addEventListener('click', async () => {
  startButton.disabled = true;
  try {
    const data = await api('/api/start', { method: 'POST', body: '{}' });
    renderStatus(data.status);
    streamWanted = true;
    streamConnected = false;
    player.load();
    await player.play().catch(() => {});
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
  $('#pureRadio').checked = radioSettings.playback_mode === 'radio';
  $('#podcastSelectorEnabled').checked = radioSettings.podcast_selector_enabled;
  $('#restartPodcastEnabled').checked = radioSettings.restart_current_podcast_enabled;
  $('#musicPlacement').value = radioSettings.music_placement;
  $('#songsPerBreak').value = radioSettings.songs_per_break;
  $('#unplayedEpisodesPerSource').value = radioSettings.unplayed_episodes_per_source;
  $('#playedEpisodesPerSource').value = radioSettings.played_episodes_per_source;
  const selectedName = radioSettings.selected_playlist_name || '';
  playlistInput.value = selectedName;
  activePlaylistName.textContent = radioSettings.seed_track_name
    || selectedName
    || radioSettings.active_music_source_name
    || 'Random saved playlist';
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

function openSettings() {
  animateSettings('open');
  settingsOverlay.hidden = false;
  settingsButton.setAttribute('aria-expanded', 'true');
  settingsButton.setAttribute('aria-label', 'Close settings');
  $('#settingsClose').focus();
}

function closeSettings() {
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
    playlistOptions.replaceChildren(...playlistCatalog.map((item) => {
      const option = document.createElement('option');
      option.value = item.name;
      option.label = item.tracks_total ? `${item.name} · ${item.tracks_total}` : item.name;
      return option;
    }));
  } catch (error) { notify(error.message, true); }
}

function hidePlaylistConfirmation() {
  proposedPlaylist = null;
  playlistConfirm.hidden = true;
}

function proposePlaylistChange() {
  const value = playlistInput.value.trim();
  const match = playlistCatalog.find((item) => item.name.toLowerCase() === value.toLowerCase());
  if (value && !match) return;
  const proposal = match || { uri: '', name: '' };
  if ((radioSettings.selected_playlist_uri || '') === proposal.uri) {
    hidePlaylistConfirmation();
    return;
  }
  proposedPlaylist = proposal;
  playlistConfirmText.textContent = `Switch to ${proposal.name || 'a random saved playlist'}?`;
  playlistConfirm.hidden = false;
}

playlistInput.addEventListener('change', proposePlaylistChange);
playlistInput.addEventListener('dblclick', () => {
  playlistInput.select();
  if (typeof playlistInput.showPicker === 'function') playlistInput.showPicker();
});
$('#playlistCancel').addEventListener('click', () => {
  playlistInput.value = radioSettings.selected_playlist_name || '';
  hidePlaylistConfirmation();
});
$('#playlistApply').addEventListener('click', async () => {
  if (!proposedPlaylist) return;
  const selection = proposedPlaylist;
  try {
    const data = await api('/api/playlist', {
      method: 'POST',
      body: JSON.stringify({ uri: selection.uri, name: selection.name }),
    });
    renderSettings(data.settings);
    hidePlaylistConfirmation();
    notify(selection.name ? `PLAYLIST: ${selection.name}` : 'PLAYLIST: RANDOM');
  } catch (error) { notify(error.message, true); }
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
    button.type = 'button';
    button.textContent = item.prepared ? 'PLAY AGAIN' : (item.preparing ? 'PREPARING…' : 'PREPARE');
    button.disabled = item.preparing;
    button.classList.toggle('primary', item.prepared);
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        if (item.prepared) {
          const data = await api('/api/history', {
            method: 'POST',
            body: JSON.stringify({ id: item.id, action: 'play_again' }),
          });
          renderSettings(data.settings);
          renderStatus(data.status);
          if (player.paused) await playPlayback();
          notify(`PLAYING AGAIN: ${item.title}`);
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
        notify(error.message, true);
      } finally {
        button.disabled = item.preparing;
        if (!item.prepared && !item.preparing) button.textContent = 'PREPARE';
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
