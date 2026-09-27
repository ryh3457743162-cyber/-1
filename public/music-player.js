/* One player for the cover, book, atlas and flat views in index.html. */
(() => {
  const audio = document.getElementById('global-music-audio');
  const shell = document.getElementById('music-control');
  if (!audio || !shell) return;
  const $ = selector => shell.querySelector(selector);
  const toggle = $('#music-toggle');
  const panel = $('#music-panel');
  const playButton = $('#music-play');
  const progress = $('#music-progress');
  const volume = $('#music-volume');
  const status = $('#music-status');
  const title = $('#music-title');
  const artist = $('#music-artist');
  const clock = $('#music-clock');
  let config = null;
  let targetVolume = 0.3;
  let fadeFrame = 0;
  let pausedByUser = false;
  let waitingForGesture = false;
  let unavailable = false;
  let starting = false;
  let startingFromGesture = false;
  let playToken = 0;
  let seekDragging = false;

  const storage = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, String(value)); } catch {} },
  };
  const time = seconds => {
    if (!Number.isFinite(seconds)) return '00:00';
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  };
  function stopFade() { if (fadeFrame) cancelAnimationFrame(fadeFrame); fadeFrame = 0; }
  function fade(to, duration, done) {
    stopFade();
    const from = audio.volume, began = performance.now();
    if (!duration) { audio.volume = to; done?.(); return; }
    function tick(now) {
      const fraction = Math.min(1, (now - began) / duration);
      audio.volume = Math.max(0, Math.min(1, from + (to - from) * fraction));
      if (fraction < 1) fadeFrame = requestAnimationFrame(tick);
      else { fadeFrame = 0; done?.(); }
    }
    fadeFrame = requestAnimationFrame(tick);
  }
  function render() {
    if (!config?.music) { shell.hidden = true; return; }
    shell.hidden = false;
    shell.classList.toggle('waiting', waitingForGesture);
    title.textContent = config.music.title;
    artist.textContent = config.music.artist || '背景音乐';
    const playing = !audio.paused;
    playButton.textContent = playing ? '暂停' : '播放';
    playButton.setAttribute('aria-label', playing ? '暂停背景音乐' : '播放背景音乐');
    toggle.setAttribute('aria-label', playing ? '背景音乐正在播放，展开控制' : '展开背景音乐控制');
    status.textContent = unavailable ? '音乐暂时不可用' : waitingForGesture ? '点击开启音乐' : playing ? '正在播放' : '已暂停';
    if (!seekDragging) progress.value = String(audio.currentTime || 0);
    progress.max = String(Number.isFinite(audio.duration) ? audio.duration : config.music.duration || 0);
    clock.textContent = `${time(audio.currentTime)} / ${time(audio.duration || config.music.duration)}`;
  }
  async function start(duration = 3000, fromGesture = false) {
    if (!config?.music || unavailable || pausedByUser || (!audio.paused && !starting) ||
        (starting && (!fromGesture || startingFromGesture))) return;
    const token = ++playToken;
    starting = true;
    startingFromGesture = fromGesture;
    stopFade();
    audio.volume = 0;
    try {
      await audio.play();
      if (token !== playToken) return;
      waitingForGesture = false;
      storage.set('music.userActivated', '1');
      fade(targetVolume, duration);
    } catch (error) {
      if (token !== playToken) return;
      if (error?.name === 'NotAllowedError') waitingForGesture = true;
      else { unavailable = true; waitingForGesture = false; }
    } finally { if (token === playToken) { starting = false; startingFromGesture = false; render(); } }
  }
  function gesture() {
    if (config?.music && !pausedByUser && (waitingForGesture || starting || audio.paused))
      void start(config.fadeInDuration || 3000, true);
  }
  function pause() {
    if (audio.paused) return;
    pausedByUser = true;
    storage.set('music.pausedByUser', '1');
    stopFade();
    fade(0, 220, () => { audio.pause(); render(); });
    render();
  }
  function resume() {
    if (!config?.music || unavailable) return;
    pausedByUser = false;
    storage.set('music.pausedByUser', '0');
    void start(750, true);
  }
  toggle.addEventListener('click', () => {
    gesture();
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
  });
  playButton.addEventListener('click', () => (audio.paused || waitingForGesture || starting) ? resume() : pause());
  volume.addEventListener('input', () => {
    stopFade();
    targetVolume = Number(volume.value) / 100;
    audio.volume = targetVolume;
    render();
  });
  progress.addEventListener('pointerdown', () => { seekDragging = true; });
  progress.addEventListener('change', () => {
    if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(audio.duration, Number(progress.value));
    seekDragging = false;
    render();
  });
  progress.addEventListener('pointerup', () => { seekDragging = false; });
  audio.addEventListener('timeupdate', () => {
    storage.set('music.currentTime', Math.floor(audio.currentTime));
    render();
  });
  audio.addEventListener('loadedmetadata', () => {
    if (storage.get('music.id') === config?.music?.id) {
      const saved = Number(storage.get('music.currentTime'));
      if (Number.isFinite(saved) && saved > 0 && saved < audio.duration - 1) audio.currentTime = saved;
    }
    render();
  });
  audio.addEventListener('play', render);
  audio.addEventListener('pause', render);
  audio.addEventListener('error', () => { unavailable = true; stopFade(); render(); });
  document.addEventListener('pointerdown', event => {
    if (!shell.contains(event.target)) gesture();
  }, { capture: true });
  document.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && !shell.contains(event.target)) gesture();
  }, { capture: true });
  window.globalMusicPlayer = { startFromGesture: gesture };
  fetch('/api/music/current', { cache: 'no-store' })
    .then(response => response.ok ? response.json() : null)
    .then(data => {
      if (!data?.enabled || !data.music) return;
      config = data;
      targetVolume = Math.max(0, Math.min(1, Number(data.volume) || 0));
      volume.value = String(Math.round(targetVolume * 100));
      audio.loop = Boolean(data.loop);
      audio.preload = 'none';
      audio.src = data.music.url;
      pausedByUser = storage.get('music.id') === data.music.id && storage.get('music.pausedByUser') === '1';
      if (storage.get('music.id') !== data.music.id) storage.set('music.currentTime', '0');
      storage.set('music.id', data.music.id);
      render();
      if (!pausedByUser) void start(data.fadeInDuration || 3000);
    })
    .catch(() => { /* Music must never stop the photo site from loading. */ });
})();
