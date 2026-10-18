// Minimal transport adapter. ALL extraction code below is imported unchanged
// from the live MediaX snapshot in ./src/*.js.
// Operational order mirrors MediaX processStandardDownload:
// resolveMedia -> preferred local upload -> candidate sendVideo(URL)/upload.
import { parseMedia, chooseBestVideo } from './src/downloader.js';
import { parseYouTubeFree } from './src/youtube-free.js';
import { prepareYouTubeTelegramUpload } from './src/youtube-upload.js';

const marker = '__MUSIX_MEDIAX_YOUTUBE_RESULT__:';
const inputUrl = String(process.argv[2] || '');
const limit = Math.max(1, Number(process.env.TELEGRAM_UPLOAD_MAX_MB || 50)) * 1024 * 1024;

function orderedVideoCandidates(videos = []) {
  // Exactly the selection/size ordering from MediaX src/features/downloader.js.
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
  const likelySendable = ordered.filter((item) => !item.filesize || Number(item.filesize) <= limit);
  const knownTooLarge = ordered.filter((item) => item.filesize && Number(item.filesize) > limit);
  return [...likelySendable, ...knownTooLarge];
}

function emit(data) {
  console.log(marker + JSON.stringify(data));
}

let media;
let resolverError = null;
try {
  media = await parseMedia(inputUrl);
} catch (error) {
  try {
    media = await parseYouTubeFree(inputUrl);
  } catch (fallbackError) {
    resolverError = String(error?.message || error) + '; free fallback: ' + String(fallbackError?.message || fallbackError);
    media = { platform: 'YouTube', title: '', videos: [], audios: [], images: [] };
  }
}

let uploadError = null;
try {
  // EXACT MediaX preferred local downloader path, same selectors and FFmpeg.
  const prepared = await prepareYouTubeTelegramUpload(inputUrl, limit);
  emit({
    status: 'local_file',
    filePath: prepared.filePath,
    quality: prepared.quality,
    title: prepared.title || media.title || '',
    compressed: Boolean(prepared.compressed),
    mediaxRevision: '08e1b510dfa7396d6b1d05561918db30c604f0c7',
  });
} catch (error) {
  uploadError = String(error?.message || error);
  // EXACT MediaX fallback: original candidate URL -> Telegram sendVideo(URL),
  // if that fails -> download + multipart upload by the Musix adapter.
  const ordered = orderedVideoCandidates(media.videos || []);
  emit({
    status: 'candidates',
    title: media.title || '',
    candidates: ordered.slice(0, 6).map((item) => ({
      url: item.url,
      quality: item.quality || '',
      width: item.width || null,
      height: item.height || null,
      filesize: item.filesize || null,
      headers: item.headers || null,
      source: item.source || '',
    })),
    resolverError,
    uploadError,
    mediaxRevision: '08e1b510dfa7396d6b1d05561918db30c604f0c7',
  });
}
