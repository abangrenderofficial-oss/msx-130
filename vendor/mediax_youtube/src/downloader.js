import { execFile } from 'node:child_process';
import { chmod } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { positiveIntEnv } from './env-number.js';

const execFileAsync = promisify(execFile);
const TIKWM_API = 'https://www.tikwm.com/api/';
const TIKWM_ORIGIN = 'https://www.tikwm.com';
const PIPED_INSTANCES = [
  'https://pipedapi.kavin.rocks',
  'https://pipedapi.leptons.xyz',
  'https://pipedapi.nosebs.ru',
  'https://pipedapi.syncpundit.io',
  'https://api-piped.mha.fi',
];

function normalizeInfo(raw) {
  if (!raw) return null;
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }
  return raw;
}

function absoluteUrl(value, origin = TIKWM_ORIGIN) {
  if (!value || typeof value !== 'string') return '';
  try {
    return new URL(value, origin).toString();
  } catch {
    return '';
  }
}

function qualityLabel(format = {}) {
  if (format.height) return `${format.height}p`;
  if (format.format_note) return String(format.format_note);
  if (format.format) return String(format.format);
  return 'video';
}

function hasAudio(format = {}) {
  return format.acodec && format.acodec !== 'none';
}

function hasVideo(format = {}) {
  return format.vcodec && format.vcodec !== 'none';
}

function usableVideoFormats(info = {}) {
  const formats = Array.isArray(info.formats) ? info.formats : [];
  const direct = formats
    .filter((f) => f?.url && hasVideo(f) && hasAudio(f))
    .map((f) => ({
      url: f.url,
      quality: qualityLabel(f),
      width: f.width ?? null,
      height: f.height ?? null,
      ext: f.ext ?? null,
      hasAudio: true,
      source: 'direct',
      headers: f.http_headers ?? info.http_headers ?? null,
      filesize: f.filesize ?? f.filesize_approx ?? null,
    }));

  if (direct.length) return direct;

  if (info.url) {
    return [{
      url: info.url,
      quality: info.height ? `${info.height}p` : 'video',
      width: info.width ?? null,
      height: info.height ?? null,
      ext: info.ext ?? null,
      hasAudio: info.acodec !== 'none',
      source: 'direct',
      headers: info.http_headers ?? null,
      filesize: info.filesize ?? info.filesize_approx ?? null,
    }];
  }

  return [];
}

async function parseTikTok(url) {
  const endpoint = new URL(TIKWM_API);
  endpoint.searchParams.set('url', url);
  endpoint.searchParams.set('hd', '1');

  let response;
  try {
    response = await fetch(endpoint, {
      headers: {
        Accept: 'application/json',
        'User-Agent': 'Mozilla/5.0 (compatible; ARDownloader/1.0)',
      },
      signal: AbortSignal.timeout(positiveIntEnv('DOWNLOADER_TIMEOUT_MS', 25000)),
    });
  } catch (error) {
    const err = new Error(error?.message || 'TikWM request failed.');
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  if (!response.ok) {
    const err = new Error(`TikWM HTTP ${response.status}`);
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    const err = new Error('TikWM returned invalid JSON.');
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  if (payload?.code !== 0 || !payload?.data) {
    const err = new Error(payload?.msg || 'TikWM could not resolve this TikTok link.');
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  const data = payload.data;
  const videos = [];
  const seen = new Set();

  const addVideo = (value, quality) => {
    const direct = absoluteUrl(value);
    if (!direct || seen.has(direct)) return;
    seen.add(direct);
    videos.push({
      url: direct,
      sourceUrl: url,
      quality,
      width: data.width ?? null,
      height: data.height ?? null,
      ext: 'mp4',
      hasAudio: true,
      source: 'direct',
      headers: null,
      filesize: null,
    });
  };

  addVideo(data.hdplay, 'HD');
  addVideo(data.play, 'No watermark');

  const images = Array.isArray(data.images)
    ? data.images
        .map((item) => ({ url: absoluteUrl(typeof item === 'string' ? item : item?.url) }))
        .filter((item) => item.url)
    : [];

  const musicUrl = absoluteUrl(data.music);
  const audios = musicUrl ? [{ url: musicUrl, quality: 'audio' }] : [];

  if (!videos.length && !images.length && !audios.length) {
    const err = new Error('No downloadable TikTok media was found.');
    err.code = 'NO_MEDIA';
    throw err;
  }

  return {
    platform: 'TikTok',
    title: data.title ?? '',
    thumbnail: absoluteUrl(data.cover || data.origin_cover),
    duration: data.duration ?? null,
    images,
    videos,
    audios,
  };
}

function youtubeIdFromUrl(input) {
  try {
    const u = new URL(input);
    if (u.hostname === 'youtu.be') return u.pathname.split('/').filter(Boolean)[0] || '';
    if (u.hostname.endsWith('youtube.com')) {
      if (u.pathname === '/watch') return u.searchParams.get('v') || '';
      const parts = u.pathname.split('/').filter(Boolean);
      if (['shorts', 'embed', 'live'].includes(parts[0])) return parts[1] || '';
    }
  } catch {}
  return '';
}

async function parseYouTubeWithPiped(url) {
  const videoId = youtubeIdFromUrl(url);
  if (!videoId) {
    const err = new Error('Could not read YouTube video ID.');
    err.code = 'NO_MEDIA';
    throw err;
  }

  const failures = [];
  for (const base of PIPED_INSTANCES) {
    try {
      const response = await fetch(`${base}/streams/${encodeURIComponent(videoId)}`, {
        headers: { Accept: 'application/json', 'User-Agent': 'ARDownloader/1.0' },
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) {
        failures.push(`${new URL(base).host}:${response.status}`);
        continue;
      }
      const data = await response.json();
      const streams = Array.isArray(data?.videoStreams) ? data.videoStreams : [];
      const combined = streams
        .filter((s) => s?.url && s.videoOnly === false)
        .map((s) => ({
          url: s.url,
          sourceUrl: url,
          quality: s.quality || (s.height ? `${s.height}p` : 'video'),
          width: s.width ?? null,
          height: s.height ?? null,
          ext: String(s.format || '').toUpperCase().includes('MPEG') || String(s.mimeType || '').includes('mp4') ? 'mp4' : null,
          hasAudio: true,
          source: 'piped',
          headers: null,
          filesize: s.contentLength ? Number(s.contentLength) : null,
        }));
      if (!combined.length) {
        failures.push(`${new URL(base).host}:no-combined-stream`);
        continue;
      }
      return {
        platform: 'YouTube',
        title: data.title || '',
        thumbnail: data.thumbnailUrl || '',
        duration: data.duration ?? null,
        images: [],
        videos: combined,
        audios: [],
      };
    } catch (error) {
      failures.push(`${new URL(base).host}:${error?.name || 'error'}`);
    }
  }

  const err = new Error(`Piped fallback failed: ${failures.join(', ')}`);
  err.code = 'DOWNLOADER_ERROR';
  throw err;
}

function decodeHtml(value = '') {
  return String(value)
    .replaceAll('&amp;', '&')
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>');
}

function readMeta(html, keys) {
  const tags = html.match(/<meta\b[^>]*>/gi) || [];
  for (const tag of tags) {
    const attrs = {};
    for (const match of tag.matchAll(/([\w:-]+)\s*=\s*(["'])(.*?)\2/gi)) {
      attrs[match[1].toLowerCase()] = decodeHtml(match[3]);
    }
    const name = String(attrs.property || attrs.name || '').toLowerCase();
    if (keys.includes(name) && attrs.content) return attrs.content;
  }
  return '';
}

function unescapeJsonUrl(value = '') {
  if (!value) return '';
  try {
    return JSON.parse(`"${value.replaceAll('"', '\\"')}"`);
  } catch {
    return value.replaceAll('\\/', '/').replaceAll('\\u0026', '&');
  }
}

async function parseThreads(url) {
  let response;
  try {
    response = await fetch(url, {
      redirect: 'follow',
      headers: {
        Accept: 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36',
      },
      signal: AbortSignal.timeout(15000),
    });
  } catch (error) {
    const err = new Error(`Threads request failed: ${error?.message || error}`);
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  if (!response.ok) {
    const err = new Error(`Threads HTTP ${response.status}`);
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }

  const html = await response.text();
  const title = readMeta(html, ['og:title', 'twitter:title']) || 'Threads media';
  const image = readMeta(html, ['og:image', 'og:image:secure_url', 'twitter:image']);
  let video = readMeta(html, ['og:video', 'og:video:secure_url', 'twitter:player:stream']);

  if (!video) {
    const candidates = [];
    for (const regex of [
      /"video_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/g,
      /"playable_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/g,
      /"url"\s*:\s*"(https?:\\?\/\\?\/[^"\\]+\.mp4[^"\\]*)"/g,
    ]) {
      for (const match of html.matchAll(regex)) candidates.push(unescapeJsonUrl(match[1]));
    }
    video = candidates.find((item) => /^https?:\/\//i.test(item)) || '';
  }

  const videos = video ? [{
    url: decodeHtml(video),
    sourceUrl: url,
    quality: 'video',
    width: null,
    height: null,
    ext: 'mp4',
    hasAudio: true,
    source: 'threads-page',
    headers: {
      Referer: response.url || 'https://www.threads.com/',
      'User-Agent': 'Mozilla/5.0',
    },
    filesize: null,
  }] : [];

  const images = !videos.length && image ? [{ url: decodeHtml(image), headers: null }] : [];
  if (!videos.length && !images.length) {
    const err = new Error(`Threads page had no public media metadata (final=${response.url}, bytes=${html.length}).`);
    err.code = 'NO_MEDIA';
    throw err;
  }

  return {
    platform: 'Threads',
    title,
    thumbnail: image || '',
    duration: null,
    images,
    videos,
    audios: [],
  };
}

async function parseWithYtDlp(url) {
  const binary = path.join(process.cwd(), 'bin', 'yt-dlp');
  try {
    await chmod(binary, 0o755).catch(() => {});
    const args = [
      '--dump-single-json',
      '--skip-download',
      '--no-warnings',
      '--no-playlist',
      '--no-check-certificates',
      '--prefer-free-formats',
      '--js-runtimes', `node:${process.execPath}`,
      '--remote-components', 'ejs:github',
      '--',
      url,
    ];

    const { stdout } = await execFileAsync(binary, args, {
      timeout: positiveIntEnv('DOWNLOADER_TIMEOUT_MS', 45000),
      maxBuffer: 12 * 1024 * 1024,
      env: { ...process.env, PATH: `${path.dirname(process.execPath)}:${process.env.PATH || ''}` },
    });

    const info = normalizeInfo(stdout);
    if (!info) {
      const err = new Error('yt-dlp returned invalid metadata.');
      err.code = 'NO_MEDIA';
      throw err;
    }

    const videos = usableVideoFormats(info).map((item) => ({ ...item, sourceUrl: url }));
    if (!videos.length) {
      const err = new Error('No directly downloadable video format was found.');
      err.code = 'NO_MEDIA';
      throw err;
    }

    return {
      platform: info.extractor_key ?? info.extractor ?? null,
      title: info.title ?? '',
      thumbnail: info.thumbnail ?? '',
      duration: info.duration ?? null,
      images: [],
      videos,
      audios: [],
    };
  } catch (error) {
    if (error?.code === 'NO_MEDIA') throw error;
    const detail = error?.stderr || error?.stdout || error?.message || 'yt-dlp failed.';
    const err = new Error(String(detail));
    err.code = 'DOWNLOADER_ERROR';
    throw err;
  }
}

export async function parseMedia(url) {
  const hostname = new URL(url).hostname.toLowerCase();

  if (hostname.endsWith('threads.net') || hostname.endsWith('threads.com')) {
    return parseThreads(url);
  }

  if (hostname === 'tiktok.com' || hostname.endsWith('.tiktok.com')) {
    try {
      return await parseTikTok(url);
    } catch (tikwmError) {
      try {
        const fallback = await parseWithYtDlp(url);
        return { ...fallback, platform: 'TikTok' };
      } catch (ytdlpError) {
        const err = new Error(`TikWM: ${tikwmError?.message || tikwmError}; yt-dlp fallback: ${ytdlpError?.message || ytdlpError}`);
        err.code = tikwmError?.code === 'NO_MEDIA' && ytdlpError?.code === 'NO_MEDIA' ? 'NO_MEDIA' : 'DOWNLOADER_ERROR';
        throw err;
      }
    }
  }

  if (hostname === 'youtu.be' || hostname.endsWith('youtube.com')) {
    try {
      return await parseWithYtDlp(url);
    } catch (error) {
      return parseYouTubeWithPiped(url).catch((fallbackError) => {
        const err = new Error(`yt-dlp: ${error?.message || error}; fallback: ${fallbackError?.message || fallbackError}`);
        err.code = 'DOWNLOADER_ERROR';
        throw err;
      });
    }
  }

  return parseWithYtDlp(url);
}

function qualityScore(value = '') {
  const text = String(value).toLowerCase();
  if (text.includes('hd')) return 10000;
  const match = text.match(/(\d{3,4})p?/i);
  return match ? Number(match[1]) : 0;
}

export function chooseBestVideo(videos = []) {
  if (!videos.length) return null;

  return [...videos].sort((a, b) => {
    const aDirectAudio = Number(a?.hasAudio !== false && a?.source === 'direct');
    const bDirectAudio = Number(b?.hasAudio !== false && b?.source === 'direct');
    if (aDirectAudio !== bDirectAudio) return bDirectAudio - aDirectAudio;

    const aAudio = Number(a?.hasAudio !== false);
    const bAudio = Number(b?.hasAudio !== false);
    if (aAudio !== bAudio) return bAudio - aAudio;

    return qualityScore(b?.quality) - qualityScore(a?.quality);
  })[0];
}

export function needsCustomHeaders(item) {
  const headers = item?.headers && typeof item.headers === 'object' ? item.headers : null;
  if (!headers) return false;
  const keys = Object.keys(headers).filter((key) => {
    const lower = key.toLowerCase();
    return lower !== 'accept-language' && lower !== 'accept';
  });
  return keys.length > 0;
}