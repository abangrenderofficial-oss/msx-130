import { parseMedia, chooseBestVideo, needsCustomHeaders } from '../downloader.js';
import { resolveTikTokEmbeddedPhoto } from '../tiktok-photo-embed.js';
import { resolveTikTokPhotoUrl, resolveTikTokSlideshow } from '../tiktok-slideshow.js';
import { positiveIntEnv } from '../env-number.js';
import { resolveInstagramAudio } from '../instagram-audio.js';
import { resolveInstagramYtDlpAudio } from '../instagram-ytdlp-audio.js';
import { resolveInstagramProviderVideo } from '../instagram-provider-fallback.js';
import { isInstagramStoryUrl, resolveInstagramStory } from '../instagram-story.js';
import { parseThreadsPost } from '../threads.js';
import { parseTwitterVideo } from '../twitter.js';
import { parseYouTubeFree } from '../youtube-free.js';

const TIKWM_API = 'https://www.tikwm.com/api/';
const TIKWM_ORIGIN = 'https://www.tikwm.com';
const TIKTOK_BROWSER_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';

function absoluteUrl(value, origin = TIKWM_ORIGIN) {
  if (!value || typeof value !== 'string') return '';
  try { return new URL(value, origin).toString(); } catch { return ''; }
}

async function tikwmRequest(attempt) {
  const browserHeaders = {
    Accept: 'application/json',
    'User-Agent': TIKTOK_BROWSER_USER_AGENT,
    Referer: 'https://www.tikwm.com/',
    Origin: 'https://www.tikwm.com',
  };
  const timeout = positiveIntEnv('DOWNLOADER_TIKWM_TIMEOUT_MS', positiveIntEnv('DOWNLOADER_TIMEOUT_MS', 25000));

  if (attempt.method === 'GET') {
    const endpoint = new URL(TIKWM_API);
    endpoint.searchParams.set('url', attempt.url);
    if (attempt.hd) endpoint.searchParams.set('hd', attempt.hd);
    const response = await fetch(endpoint, {
      headers: browserHeaders,
      signal: AbortSignal.timeout(timeout),
    });
    if (!response.ok) throw new Error(`TikWM HTTP ${response.status}`);
    return response.json();
  }

  const form = new URLSearchParams();
  form.set('url', attempt.url);
  if (attempt.hd) form.set('hd', attempt.hd);
  const response = await fetch(TIKWM_API, {
    method: 'POST',
    headers: { ...browserHeaders, 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
    body: form.toString(),
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) throw new Error(`TikWM HTTP ${response.status}`);
  return response.json();
}

function tikwmRetryUrls(url) {
  const retryUrls = [url];
  try {
    const input = new URL(url);
    const shortMatch = input.hostname.match(/^(?:vt|vm)\.tiktok\.com$/i) && input.pathname.match(/^\/([^/?#]+)/);
    const code = shortMatch?.[1] || '';
    if (code) {
      for (const candidate of [
        `https://${input.hostname}/${code}/`,
        `https://www.tiktok.com/t/${code}/`,
        `https://www.tiktok.com/t/${code}`,
      ]) {
        if (!retryUrls.includes(candidate)) retryUrls.push(candidate);
      }
    }
  } catch {}
  return retryUrls;
}

async function resolveTikTok(url) {
  // TikTok Photo Mode can be embedded publicly even when TikWM is blocked
  // by an upstream 403. Detect Photo Mode before video-only providers.
  try {
    const photo = await resolveTikTokEmbeddedPhoto(url);
    if (photo) {
      console.info('[tiktok-photo] public embed resolved', { images: photo.images.length, hasAudio: Boolean(photo.audio?.url) });
      return photo;
    }
  } catch (error) {
    console.warn('[tiktok-photo] public embed unavailable; falling back to TikWM:', error?.message);
  }
  // TikTok Photo Mode has images and a separate soundtrack, not an MP4.
  // Recognize short share URLs before invoking video-only resolver fallbacks.
  const photoUrl = await resolveTikTokPhotoUrl(url);
  if (photoUrl) {
    try {
      const slideshow = await resolveTikTokSlideshow(photoUrl);
      return {
        platform: 'TikTok',
        title: slideshow.title || '',
        thumbnail: slideshow.images[0]?.url || '',
        duration: slideshow.duration,
        canonicalUrl: photoUrl,
        images: slideshow.images,
        videos: [],
        audios: [slideshow.audio],
      };
    } catch (error) {
      console.warn('[tiktok-photo] native Photo extraction failed; trying video providers:', error?.message);
    }
  }

  const attempts = [{ method: 'GET', url, hd: '1' }];
  for (const retryUrl of tikwmRetryUrls(url)) {
    attempts.push({ method: 'GET', url: retryUrl, hd: '' });
    attempts.push({ method: 'POST', url: retryUrl, hd: '0' });
    attempts.push({ method: 'POST', url: retryUrl, hd: '1' });
  }

  let payload = null;
  let lastError = null;
  for (let index = 0; index < attempts.length; index += 1) {
    try {
      const candidate = await tikwmRequest(attempts[index]);
      if (candidate?.code === 0 && candidate?.data) {
        payload = candidate;
        break;
      }
      lastError = new Error(candidate?.msg || 'TikWM could not resolve this TikTok link.');
    } catch (error) {
      lastError = error;
    }
    if (index < attempts.length - 1) await new Promise((resolve) => setTimeout(resolve, 350));
  }

  if (!payload?.data) {
    try {
      const fallback = await parseMedia(url);
      return { ...fallback, platform: 'TikTok', canonicalUrl: fallback?.canonicalUrl || url };
    } catch (fallbackError) {
      const error = new Error(`TikWM: ${lastError?.message || 'failed'}; fallback: ${fallbackError?.message || fallbackError}`);
      error.code = fallbackError?.code || 'DOWNLOADER_ERROR';
      throw error;
    }
  }

  const data = payload.data;
  const authorHandle = String(data?.author?.unique_id || data?.author?.uniqueId || '').trim().replace(/^@/, '');
  const videoId = String(data?.id || '').trim();
  const canonicalUrl = videoId
    ? `https://www.tiktok.com/@${encodeURIComponent(authorHandle || '_')}/video/${encodeURIComponent(videoId)}`
    : url;
  const headers = {
    'User-Agent': TIKTOK_BROWSER_USER_AGENT,
    Referer: canonicalUrl || 'https://www.tiktok.com/',
    Accept: 'video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8',
  };

  const videos = [];
  const seen = new Set();
  const addVideo = (value, quality) => {
    const direct = absoluteUrl(value);
    if (!direct || seen.has(direct)) return;
    seen.add(direct);
    videos.push({
      url: direct,
      sourceUrl: canonicalUrl,
      quality,
      width: data.width ?? null,
      height: data.height ?? null,
      ext: 'mp4',
      hasAudio: true,
      source: 'direct',
      headers,
      filesize: null,
    });
  };
  addVideo(data.hdplay, 'HD');
  addVideo(data.play, 'No watermark');

  const images = Array.isArray(data.images)
    ? data.images.map((item) => ({ url: absoluteUrl(typeof item === 'string' ? item : item?.url) })).filter((item) => item.url)
    : [];
  const musicUrl = absoluteUrl(data.music);
  const audios = musicUrl ? [{ url: musicUrl, quality: 'audio' }] : [];

  if (!videos.length && !images.length && !audios.length) {
    const error = new Error('No downloadable TikTok media was found.');
    error.code = 'NO_MEDIA';
    throw error;
  }

  return {
    platform: 'TikTok',
    title: data.title ?? '',
    thumbnail: absoluteUrl(data.cover || data.origin_cover),
    duration: data.duration ?? null,
    canonicalUrl,
    images,
    videos,
    audios,
  };
}

async function resolveInstagram(url) {
  // Story URLs use a different Instagram delivery path from Reels/posts. Resolve
  // the exact Story item first so yt-dlp/Reel-specific fallbacks do not reject it.
  if (isInstagramStoryUrl(url)) {
    console.info('[instagram-resolver] Instagram Story URL detected; using Story resolver');
    return resolveInstagramStory(url);
  }

  const media = await parseMedia(url);
  const canonicalUrl = media?.canonicalUrl || url;
  const videos = Array.isArray(media?.videos) ? media.videos.filter((item) => item?.url) : [];
  const existingAudios = Array.isArray(media?.audios)
    ? media.audios.filter((item) => item?.url)
    : [];

  // Normal Reel: source video already has audio. Leave it completely unchanged.
  if (videos.some((item) => item?.hasAudio === true)) {
    console.info('[instagram-resolver] embedded audio detected; native video flow unchanged');
    return { ...media, platform: 'Instagram', canonicalUrl, audios: existingAudios };
  }

  if (existingAudios.length) {
    console.info('[instagram-resolver] silent video + existing Reel sound stream detected');
    return { ...media, platform: 'Instagram', canonicalUrl, audios: existingAudios };
  }

  // For Reels where Instagram exposes only a silent/VP9 video track, prefer the
  // provider's already-merged H.264 + AAC MP4 directly. This is faster, avoids a
  // second local mux, and is playable on iPhone/Telegram.
  try {
    const merged = await resolveInstagramProviderVideo(canonicalUrl);
    if (merged?.url) {
      console.info('[instagram-resolver] merged provider Reel selected as primary playable video');
      return {
        ...media,
        platform: 'Instagram',
        canonicalUrl,
        videos: [{ ...merged, hasAudio: true }],
        audios: [],
      };
    }
  } catch (error) {
    console.warn('[instagram-resolver] merged Reel provider failed:', error?.code, error?.message);
  }

  // Fallback: try Instagram/yt-dlp's standalone sound stream. The downloader
  // muxer will transcode non-H.264 video to an iPhone-compatible H.264 MP4.
  try {
    const sound = await resolveInstagramYtDlpAudio(canonicalUrl);
    if (sound?.url) {
      console.info('[instagram-resolver] silent video + standalone Reel sound resolved');
      return {
        ...media,
        platform: 'Instagram',
        canonicalUrl,
        audios: [sound],
      };
    }
  } catch (error) {
    console.warn('[instagram-resolver] standalone Reel sound not resolved:', error?.code, error?.message);
  }

  // Last fallback: legacy metadata sound resolver.
  try {
    const sound = await resolveInstagramAudio(canonicalUrl);
    if (sound?.url) {
      console.info('[instagram-resolver] metadata Reel sound resolved');
      return {
        ...media,
        platform: 'Instagram',
        canonicalUrl,
        audios: [sound],
      };
    }
  } catch (error) {
    console.warn('[instagram-resolver] metadata Reel sound not resolved:', error?.code, error?.message);
  }

  // Never send a known-silent Instagram candidate to Telegram. Apart from
  // missing audio, these target Reels can be VP9-in-MP4 and fail playback on iOS.
  const error = new Error('Instagram Reel audio could not be recovered; refusing to send a silent/non-iPhone-compatible copy.');
  error.code = 'INSTAGRAM_AUDIO_NOT_FOUND';
  throw error;
}

export async function resolveMedia(platform, url) {
  if (platform === 'threads') return parseThreadsPost(url);
  if (platform === 'twitter') return parseTwitterVideo(url);
  if (platform === 'tiktok') return resolveTikTok(url);
  if (platform === 'instagram') return resolveInstagram(url);
  if (platform === 'youtube') {
    try {
      return await parseMedia(url);
    } catch (primaryError) {
      try {
        return await parseYouTubeFree(url);
      } catch (fallbackError) {
        const error = new Error(`yt-dlp: ${primaryError?.message || primaryError}; free fallback: ${fallbackError?.message || fallbackError}`);
        error.code = primaryError?.code === 'NO_MEDIA' && fallbackError?.code === 'NO_MEDIA' ? 'NO_MEDIA' : 'DOWNLOADER_ERROR';
        throw error;
      }
    }
  }
  return parseMedia(url);
}

export { chooseBestVideo, needsCustomHeaders };
