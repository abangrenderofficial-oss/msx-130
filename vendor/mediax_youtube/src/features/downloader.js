import { prepareSocialVideoTelegramUpload } from '../social-video.js';
import { prepareInstagramVideoWithAudio } from '../instagram-video-audio-mux.js';
import { resolveInstagramProviderVideo } from '../instagram-provider-fallback.js';
import { prepareYouTubeTelegramUpload } from '../youtube-upload.js';
import { prepareTikTokTelegramRescue } from '../tiktok-rescue.js';
import { createRelayUrl } from '../relay.js';
import { platformLabel } from '../platform.js';
import { isJobFenceActive } from '../recovery.js';
import {
  sendChatAction,
  sendDownloadButton,
  sendMediaGroup,
  sendMessage,
  sendPhotoUrl,
  sendVideoFileUpload,
  sendVideoUpload,
  sendVideoUrl,
} from '../telegram.js';
import { mediaActionButtons } from '../bot/media-actions.js';
import { localMediaLane } from '../bot/job-lanes.js';
import { mirrorMediaToGroup } from '../bot/audit.js';
import { chooseBestVideo, needsCustomHeaders, resolveMedia } from '../bot/media-resolver.js';

const TELEGRAM_URL_FETCH_MAX = 20 * 1024 * 1024;
const TELEGRAM_CLOUD_UPLOAD_MAX = 50 * 1024 * 1024;

function configuredUploadLimit() {
  const custom = Number(process.env.TELEGRAM_UPLOAD_MAX_MB || 0);
  if (Number.isFinite(custom) && custom > 0) return Math.floor(custom * 1024 * 1024);
  return TELEGRAM_CLOUD_UPLOAD_MAX;
}

function safeTitle(media, platform) {
  const title = String(media?.title || '').trim();
  return title ? `${platformLabel(platform)} • ${title}`.slice(0, 760) : `${platformLabel(platform)} download`;
}

function relayItem(baseUrl, item) {
  if (!baseUrl || !item?.url) return null;
  try {
    return { ...item, url: createRelayUrl(baseUrl, item), headers: null };
  } catch (error) {
    console.warn('[downloader/relay] unavailable:', error?.message);
    return null;
  }
}

function orderedVideoCandidates(videos = []) {
  const pool = videos.filter((item) => item?.url);
  const ordered = [];
  while (pool.length) {
    const best = chooseBestVideo(pool);
    if (!best) break;
    ordered.push(best);
    const index = pool.indexOf(best);
    if (index >= 0) pool.splice(index, 1);
    else break;
  }

  const limit = configuredUploadLimit();
  const likelySendable = ordered.filter((item) => !item.filesize || Number(item.filesize) <= limit);
  const knownTooLarge = ordered.filter((item) => item.filesize && Number(item.filesize) > limit);
  return [...likelySendable, ...knownTooLarge];
}

function telegramVideoExtra(video, options = {}) {
  const width = Math.round(Number(video?.width || 0));
  const height = Math.round(Number(video?.height || 0));
  const durationValue = Number(video?.duration || options.duration || 0);
  const duration = durationValue > 0 ? Math.max(1, Math.round(durationValue)) : 0;

  return {
    ...mediaActionButtons(options.sourceUrl),
    ...(width > 0 ? { width } : {}),
    ...(height > 0 ? { height } : {}),
    ...(duration > 0 ? { duration } : {}),
    supports_streaming: true,
  };
}

function shouldProbeActualSocialUpload(platform) {
  return ['tiktok', 'instagram', 'threads', 'twitter'].includes(String(platform || '').toLowerCase());
}

async function sendImageFallback(chatId, images, title) {
  const buttons = images.slice(0, 20).map((image, index) => [
    { text: `⬇️ Download image ${index + 1}`, url: image.url },
  ]);
  await sendMessage(chatId, `${title}\n\nTelegram tak dapat masukkan album ini terus dalam chat. Guna butang di bawah:`, {
    reply_markup: { inline_keyboard: buttons },
  });
}

async function deliverImages(chatId, images, title, baseUrl) {
  const prepared = images.map((item) => needsCustomHeaders(item) ? relayItem(baseUrl, item) : item);
  if (prepared.some((item) => !item)) return sendImageFallback(chatId, images, title);

  if (prepared.length === 1) {
    try {
      await sendPhotoUrl(chatId, prepared[0].url, title);
      return;
    } catch {
      return sendImageFallback(chatId, images, title);
    }
  }

  for (let offset = 0; offset < prepared.length; offset += 10) {
    const chunk = prepared.slice(offset, offset + 10).map((item, index) => ({
      type: 'photo',
      media: item.url,
      ...(offset === 0 && index === 0 ? { caption: title } : {}),
    }));
    try {
      await sendMediaGroup(chatId, chunk);
    } catch {
      await sendImageFallback(chatId, images, title);
      return;
    }
  }
}

async function deliverCompressedSocial(chatId, video, durationHint, sourceUrl) {
  let prepared = null;
  try {
    prepared = await localMediaLane(() => prepareSocialVideoTelegramUpload(video, configuredUploadLimit(), {
      duration: Number(video?.duration || durationHint || 0) || null,
      sourceUrl,
    }));
    return await sendVideoFileUpload(chatId, prepared.filePath, '', mediaActionButtons(sourceUrl));
  } catch (error) {
    console.warn('[downloader/social-compress] failed:', error?.code, error?.message);
    return null;
  } finally {
    if (prepared?.cleanup) await prepared.cleanup().catch(() => {});
  }
}

async function deliverInstagramMuxed(chatId, video, audio, options = {}) {
  let prepared = null;
  try {
    prepared = await localMediaLane(() => prepareInstagramVideoWithAudio(
      video,
      audio,
      configuredUploadLimit(),
      { sourceUrl: options.sourceUrl },
    ));
    return await sendVideoFileUpload(
      chatId,
      prepared.filePath,
      '',
      telegramVideoExtra(video, options),
    );
  } catch (error) {
    console.warn('[downloader/instagram-mux] failed:', error?.code, error?.message);
    return null;
  } finally {
    if (prepared?.cleanup) await prepared.cleanup().catch(() => {});
  }
}

async function deliverVideo(chatId, video, baseUrl, options = {}) {
  if (!video?.url) return null;
  const size = Number(video.filesize || 0);
  const customHeaders = needsCustomHeaders(video);
  const relay = relayItem(baseUrl, video);
  const uploadLimit = configuredUploadLimit();
  const allowSocialCompression = options.allowCompression !== false && options.platform && options.platform !== 'youtube';
  const sendExtra = telegramVideoExtra(video, options);
  const probeActualUpload = shouldProbeActualSocialUpload(options.platform);

  if (!probeActualUpload && (!size || size <= TELEGRAM_URL_FETCH_MAX)) {
    const fetchUrl = customHeaders ? relay?.url : video.url;
    if (fetchUrl) {
      try {
        return await sendVideoUrl(chatId, fetchUrl, '', sendExtra);
      } catch (error) {
        console.warn('[downloader] Telegram URL fetch failed, trying server upload:', error?.message);
      }
    }
  }

  if (size && size > uploadLimit) {
    if (allowSocialCompression) {
      return deliverCompressedSocial(chatId, video, options.duration, options.sourceUrl);
    }
    return null;
  }

  try {
    return await sendVideoUpload(chatId, video, '', sendExtra);
  } catch (error) {
    console.warn('[downloader] Telegram server upload failed:', error?.code, error?.message);
    if (allowSocialCompression) {
      return deliverCompressedSocial(chatId, video, options.duration, options.sourceUrl);
    }
    return null;
  }
}

async function deliverPreferredYouTube(chatId, url) {
  let prepared = null;
  try {
    prepared = await localMediaLane(() => prepareYouTubeTelegramUpload(url, configuredUploadLimit()));
    return await sendVideoFileUpload(chatId, prepared.filePath, '', mediaActionButtons(url));
  } catch (error) {
    console.warn('[downloader/youtube] preferred pipeline failed:', error?.code, error?.message);
    return null;
  } finally {
    if (prepared?.cleanup) await prepared.cleanup().catch(() => {});
  }
}

function active(fence) {
  return !fence || isJobFenceActive(fence);
}

export async function processStandardDownload({ chatId, url, platform, context = {}, message = null }) {
  const fence = context.fence || null;
  await sendChatAction(chatId, 'typing').catch(() => {});

  let media;
  let initialDownloaderError = null;
  try {
    media = await resolveMedia(platform, url);
  } catch (error) {
    console.error('[downloader/resolve] error:', error?.code, error?.message);
    if (platform === 'youtube') {
      initialDownloaderError = error;
      media = { platform: 'YouTube', title: '', thumbnail: '', duration: null, images: [], videos: [], audios: [] };
    } else {
      await sendMessage(
        chatId,
        error?.code === 'NO_MEDIA'
          ? 'Tak jumpa video/media yang boleh dimuat turun. Pastikan post itu boleh diakses dan masih wujud.'
          : '❌ Tak berjaya proses link tu. Cuba link asal atau cuba semula kemudian.',
      );
      return { handled: true, slideshow: false };
    }
  }

  if (!active(fence)) return { handled: true, cancelled: true };
  if (platform === 'tiktok' && Array.isArray(media.images) && media.images.length) {
    return { handled: false, slideshow: true, media };
  }

  const title = safeTitle(media, platform);
  const candidates = orderedVideoCandidates(media.videos || []);
  const instagramAudio = platform === 'instagram' && Array.isArray(media.audios)
    ? media.audios.find((item) => item?.url) || null
    : null;
  const instagramNeedsMux = Boolean(instagramAudio?.url && candidates.length);
  let sentVideo = null;

  if (platform === 'youtube') {
    await sendChatAction(chatId, 'upload_video').catch(() => {});
    sentVideo = await deliverPreferredYouTube(chatId, url);
  }

  if (instagramNeedsMux && !sentVideo) {
    await sendChatAction(chatId, 'upload_video').catch(() => {});
    for (const candidate of candidates.slice(0, 4)) {
      if (!active(fence)) return { handled: true, cancelled: true };
      sentVideo = await deliverInstagramMuxed(chatId, candidate, instagramAudio, {
        platform,
        duration: media.duration,
        sourceUrl: media?.canonicalUrl || url,
      });
      if (sentVideo) break;
    }

    // Never fall through to the silent native candidate after we have proved the
    // Reel uses a separate social-audio stream. If local muxing fails, use a
    // provider's already-merged MP4 as the non-silent fallback instead.
    if (!sentVideo && active(fence)) {
      try {
        const merged = await resolveInstagramProviderVideo(media?.canonicalUrl || url);
        sentVideo = await deliverVideo(chatId, merged, context.baseUrl, {
          platform,
          duration: media.duration,
          sourceUrl: media?.canonicalUrl || url,
          allowCompression: false,
        });
      } catch (error) {
        console.warn('[downloader/instagram-provider] merged fallback failed after mux:', error?.code, error?.message);
      }
    }

    if (!sentVideo && active(fence)) {
      await sendMessage(chatId, '❌ Audio Instagram berjaya dikesan tetapi video+audio belum berjaya digabungkan. Cuba hantar semula link yang sama.');
    }
  }

  if (candidates.length && !sentVideo && !instagramNeedsMux) {
    await sendChatAction(chatId, 'upload_video').catch(() => {});
    for (const candidate of candidates.slice(0, 6)) {
      if (!active(fence)) return { handled: true, cancelled: true };
      sentVideo = await deliverVideo(chatId, candidate, context.baseUrl, {
        platform,
        duration: media.duration,
        sourceUrl: media?.canonicalUrl || url,
        allowCompression: false,
      });
      if (sentVideo) break;
    }

    if (!sentVideo && platform === 'tiktok' && active(fence)) {
      const compressionCandidate = chooseBestVideo(media.videos || []);
      if (compressionCandidate) {
        sentVideo = await deliverCompressedSocial(
          chatId,
          compressionCandidate,
          media.duration,
          media?.canonicalUrl || url,
        );
      }
    }

    if (!sentVideo && platform === 'tiktok' && active(fence)) {
      let rescued = null;
      try {
        rescued = await localMediaLane(() => prepareTikTokTelegramRescue(media?.canonicalUrl || url, configuredUploadLimit()));
        if (active(fence)) {
          sentVideo = await sendVideoFileUpload(chatId, rescued.filePath, '', mediaActionButtons(media?.canonicalUrl || url));
        }
      } catch (error) {
        console.warn('[downloader/tiktok-rescue] failed:', error?.code, error?.message);
      } finally {
        if (rescued?.cleanup) await rescued.cleanup().catch(() => {});
      }
    }

    if (!sentVideo && active(fence)) {
      if (platform === 'tiktok') {
        await sendMessage(chatId, '❌ Video TikTok ini belum berjaya dihantar terus. Cuba hantar semula link yang sama sekali lagi.');
      } else {
        const best = chooseBestVideo(media.videos || []);
        if (best) {
          await sendDownloadButton(
            chatId,
            `${title}\n\nBot dah cuba direct URL, relay, server upload dan HQ compression tetapi fail ini masih tidak dapat dihantar melalui Telegram cloud.`,
            best.url,
            `⬇️ Download ${best.quality || 'video'}`,
          );
        }
      }
    }
  }

  if (!active(fence)) return { handled: true, cancelled: true };
  if (sentVideo) {
    await mirrorMediaToGroup(chatId, sentVideo, context.mirrorGroupId, message?.from || {}, {
      sourceUrl: url,
      platform,
      sourceMessageId: message?.message_id,
      sourceTimestamp: message?.date,
    });
  }

  if (platform === 'youtube' && !sentVideo && !candidates.length) {
    const detail = String(initialDownloaderError?.message || '');
    const loginRequired = /private|sign in|login|members.only|authentication|cookies/i.test(detail);
    await sendMessage(
      chatId,
      loginRequired
        ? '❌ Video ini perlukan login/permission akaun. Unlisted biasa yang boleh dibuka oleh sesiapa dengan link adalah disokong.'
        : '❌ YouTube tak dapat dimuat turun kali ini. Cuba pastikan link penuh masih aktif.',
    );
    return { handled: true, slideshow: false };
  }

  if (media.images?.length) {
    await sendChatAction(chatId, 'upload_photo').catch(() => {});
    await deliverImages(chatId, media.images, sentVideo ? `${platformLabel(platform)} images` : title, context.baseUrl);
  }

  if (!candidates.length && !media.images?.length && media.audios?.length) {
    const audio = media.audios[0];
    await sendDownloadButton(chatId, `${title}\n\nAudio tersedia:`, audio.url, '🎵 Download audio');
  }

  return { handled: true, slideshow: false, media, sentVideo };
}
