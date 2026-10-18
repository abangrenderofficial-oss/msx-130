import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import ffmpegPath from 'ffmpeg-static';
import {
  prepareChannelPolicyLedger,
  recordChannelPolicyEvent,
} from './bot/channel-policy.js';

const execFileAsync = promisify(execFile);
const DEFAULT_TELEGRAM_API_BASE = 'https://api.telegram.org';
const DEFAULT_CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024;
const CHANNEL_PROMOTION_PURPOSE = Symbol('support-promotion-only');
const CHANNEL_RESET_PURPOSE = Symbol('channel-reset-delete-only');
const CHAT_TYPE_CACHE_TTL_MS = 10 * 60 * 1000;
const chatTypeCache = new Map();

const CHAT_WRITE_METHODS = new Set([
  'sendMessage',
  'sendPhoto',
  'sendVideo',
  'sendAudio',
  'sendDocument',
  'sendAnimation',
  'sendVoice',
  'sendVideoNote',
  'sendSticker',
  'sendMediaGroup',
  'sendPoll',
  'sendDice',
  'sendLocation',
  'sendVenue',
  'sendContact',
  'sendChatAction',
  'copyMessage',
  'forwardMessage',
  'editMessageText',
  'editMessageCaption',
  'editMessageMedia',
  'editMessageReplyMarkup',
  'deleteMessage',
]);

function botToken() {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    const err = new Error('Telegram bot token is not configured.');
    err.code = 'TELEGRAM_NOT_CONFIGURED';
    throw err;
  }
  return token;
}

function telegramApiBase() {
  return String(process.env.TELEGRAM_API_BASE_URL || DEFAULT_TELEGRAM_API_BASE).replace(/\/$/, '');
}

function telegramEndpoint(method) {
  return `${telegramApiBase()}/bot${botToken()}/${method}`;
}

function uploadLimitBytes() {
  const configured = Number(process.env.TELEGRAM_UPLOAD_MAX_MB || 0);
  if (Number.isFinite(configured) && configured > 0) return Math.floor(configured * 1024 * 1024);
  return DEFAULT_CLOUD_UPLOAD_LIMIT;
}

function productionBaseUrl() {
  const explicit = String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '');
  if (explicit) return explicit;
  const productionHost = String(process.env.VERCEL_PROJECT_PRODUCTION_URL || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
  if (productionHost) return `https://${productionHost}`;
  const deploymentHost = String(process.env.VERCEL_URL || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
  return deploymentHost ? `https://${deploymentHost}` : '';
}

function sourceHeaders(headers) {
  const source = headers && typeof headers === 'object' ? headers : {};
  const allowed = new Set(['user-agent', 'referer', 'origin', 'accept', 'accept-language']);
  const out = {};
  for (const [key, value] of Object.entries(source)) {
    if (!allowed.has(String(key).toLowerCase())) continue;
    if (typeof value !== 'string' || !value) continue;
    out[key] = value;
  }
  return out;
}

function extensionFor(item, contentType = '') {
  const explicit = String(item?.ext || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  if (explicit) return explicit;
  if (String(contentType).includes('webm')) return 'webm';
  if (String(contentType).includes('quicktime')) return 'mov';
  return 'mp4';
}

function appendFormExtra(form, extra = {}) {
  for (const [key, value] of Object.entries(extra || {})) {
    if (value === undefined || value === null) continue;
    if (typeof value === 'object') form.set(key, JSON.stringify(value));
    else form.set(key, String(value));
  }
}

function normalizedRotation(stderr = '') {
  const sideData = String(stderr).match(/rotation of\s+(-?\d+(?:\.\d+)?)\s+degrees/i);
  const metadata = String(stderr).match(/\brotate\s*:\s*(-?\d+(?:\.\d+)?)/i);
  const raw = Number(sideData?.[1] ?? metadata?.[1] ?? 0);
  if (!Number.isFinite(raw)) return 0;
  return ((Math.round(raw) % 360) + 360) % 360;
}

async function probeTelegramVideoMetadata(filePath) {
  if (!filePath || !ffmpegPath) return {};
  let stderr = '';
  try {
    await execFileAsync(
      ffmpegPath,
      ['-hide_banner', '-i', filePath],
      {
        timeout: Number(process.env.TELEGRAM_VIDEO_PROBE_TIMEOUT_MS || 12000),
        maxBuffer: 4 * 1024 * 1024,
      },
    );
  } catch (error) {
    stderr = String(error?.stderr || error?.message || '');
  }

  const dimensions = stderr.match(/Video:[^\n]*?\b(\d{2,5})x(\d{2,5})\b/i);
  const durationMatch = stderr.match(/Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)/i);
  let width = dimensions ? Number(dimensions[1]) : 0;
  let height = dimensions ? Number(dimensions[2]) : 0;
  const rotation = normalizedRotation(stderr);
  if ((rotation === 90 || rotation === 270) && width > 0 && height > 0) {
    [width, height] = [height, width];
  }
  const duration = durationMatch
    ? Math.max(1, Math.round((Number(durationMatch[1]) * 3600) + (Number(durationMatch[2]) * 60) + Number(durationMatch[3])))
    : 0;

  return {
    ...(width > 0 ? { width } : {}),
    ...(height > 0 ? { height } : {}),
    ...(duration > 0 ? { duration } : {}),
  };
}

async function probeTelegramVideoBuffer(buffer, extension = 'mp4') {
  if (!buffer?.length || !ffmpegPath) return {};
  const dir = await mkdtemp(path.join(os.tmpdir(), 'telegram-video-probe-'));
  const safeExtension = String(extension || 'mp4').toLowerCase().replace(/[^a-z0-9]/g, '') || 'mp4';
  const filePath = path.join(dir, `source.${safeExtension}`);
  try {
    await writeFile(filePath, buffer);
    return await probeTelegramVideoMetadata(filePath);
  } finally {
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

async function parseTelegramResponse(response, method) {
  const result = await response.json().catch(() => null);
  if (!response.ok || !result?.ok) {
    const err = new Error(result?.description || `Telegram ${method} failed (${response.status}).`);
    err.code = 'TELEGRAM_API_ERROR';
    err.status = response.status;
    throw err;
  }
  return result.result;
}

async function rawTelegram(method, payload = {}) {
  const response = await fetch(telegramEndpoint(method), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(25000),
  });
  return parseTelegramResponse(response, method);
}

function privateChatId(chatId) {
  return /^\d+$/.test(String(chatId || '').trim());
}

async function destinationInfo(chatId) {
  const key = String(chatId || '').trim();
  if (!key) return { id: '', type: '' };
  if (privateChatId(key)) return { id: key, type: 'private' };

  const cached = chatTypeCache.get(key);
  if (cached && Date.now() - cached.at < CHAT_TYPE_CACHE_TTL_MS) return cached.value;

  let chat;
  try {
    chat = await rawTelegram('getChat', { chat_id: chatId });
  } catch (error) {
    const blocked = new Error('Telegram destination could not be verified. Channel/group write blocked fail-closed.');
    blocked.code = 'TELEGRAM_DESTINATION_UNVERIFIED';
    blocked.cause = error;
    throw blocked;
  }

  const value = {
    id: String(chat?.id || key),
    type: String(chat?.type || ''),
    username: chat?.username ? `@${String(chat.username).replace(/^@/, '')}` : '',
  };
  chatTypeCache.set(key, { at: Date.now(), value });
  if (value.id) chatTypeCache.set(value.id, { at: Date.now(), value });
  if (value.username) chatTypeCache.set(value.username, { at: Date.now(), value });
  return value;
}

async function assertChatWriteAllowed(method, payload = {}, purpose = null) {
  if (!CHAT_WRITE_METHODS.has(String(method || ''))) return null;
  const chatId = payload?.chat_id;
  if (chatId === undefined || chatId === null || chatId === '') return null;

  const destination = await destinationInfo(chatId);
  if (destination.type !== 'channel') return destination;

  const supportPromotion = purpose === CHANNEL_PROMOTION_PURPOSE && method === 'sendMessage';
  const safetyDelete = purpose === CHANNEL_RESET_PURPOSE && method === 'deleteMessage';
  if (supportPromotion || safetyDelete) return destination;

  void recordChannelPolicyEvent({
    chatId: destination.id || chatId,
    method,
    purpose: 'BLOCKED_NON_PROMOTION',
    status: 'BLOCKED',
    detail: 'Downloader channel firewall denied a non-support-promotion channel write.',
  }).catch(() => {});

  const error = new Error('Downloader Bot channel firewall: only support promotion posts are allowed in channels.');
  error.code = 'DOWNLOADER_CHANNEL_WRITE_BLOCKED';
  throw error;
}

async function telegramWithPolicy(method, payload = {}, purpose = null) {
  await assertChatWriteAllowed(method, payload, purpose);
  return rawTelegram(method, payload);
}

export async function telegram(method, payload = {}) {
  return telegramWithPolicy(method, payload, null);
}

export async function getTelegramChat(chatId) {
  return destinationInfo(chatId);
}

export async function sendSupportPromotionToChannel(chatId, text, extra = {}) {
  await prepareChannelPolicyLedger();
  const destination = await destinationInfo(chatId);
  if (destination.type !== 'channel') {
    const error = new Error('Support promotion target is not a Telegram channel.');
    error.code = 'SUPPORT_PROMOTION_TARGET_NOT_CHANNEL';
    throw error;
  }

  const result = await telegramWithPolicy('sendMessage', {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
    ...extra,
  }, CHANNEL_PROMOTION_PURPOSE);

  await recordChannelPolicyEvent({
    chatId: destination.id || chatId,
    messageId: result?.message_id || '',
    method: 'sendMessage',
    purpose: 'SUPPORT_PROMOTION',
    status: 'SENT',
    detail: 'Authorized downloader support promotion.',
  }).catch((error) => {
    console.warn('[channel-firewall] promotion ledger write failed:', error?.message);
  });
  return result;
}

export async function deleteChannelMessageForSafety(chatId, messageId) {
  const destination = await destinationInfo(chatId);
  if (destination.type !== 'channel') {
    const error = new Error('Safety delete target is not a Telegram channel.');
    error.code = 'CHANNEL_RESET_TARGET_NOT_CHANNEL';
    throw error;
  }
  return telegramWithPolicy('deleteMessage', {
    chat_id: destination.id || chatId,
    message_id: Number(messageId),
  }, CHANNEL_RESET_PURPOSE);
}

async function ensureInteractiveWebhook() {
  const baseUrl = productionBaseUrl();
  const webhookSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (!baseUrl || !process.env.TELEGRAM_BOT_TOKEN) return false;

  try {
    const current = await telegram('getWebhookInfo').catch(() => null);
    const next = new URL(`${baseUrl}/api/telegram`);
    if (current?.url) {
      try {
        const existing = new URL(current.url);
        const mirrorGroup = existing.searchParams.get('mirror_group');
        if (mirrorGroup) next.searchParams.set('mirror_group', mirrorGroup);
      } catch {}
    }

    await telegram('setWebhook', {
      url: next.toString(),
      ...(webhookSecret ? { secret_token: webhookSecret } : {}),
      allowed_updates: ['message', 'edited_message', 'callback_query'],
      drop_pending_updates: false,
    });
    return true;
  } catch (error) {
    console.warn('Interactive webhook refresh failed:', error?.message);
    return false;
  }
}

export async function getTelegramFileSource(fileId) {
  if (!fileId) {
    const err = new Error('Telegram file_id is missing.');
    err.code = 'TELEGRAM_FILE_ID_MISSING';
    throw err;
  }

  const file = await telegram('getFile', { file_id: fileId });
  if (!file?.file_path) {
    const err = new Error('Telegram did not return a file path.');
    err.code = 'TELEGRAM_FILE_PATH_MISSING';
    throw err;
  }

  const ext = path.extname(file.file_path).replace(/^\./, '').toLowerCase() || 'mp4';
  return {
    url: `${telegramApiBase()}/file/bot${botToken()}/${file.file_path}`,
    ext,
    filesize: Number(file.file_size || 0) || null,
    headers: null,
    quality: 'Telegram video',
    hasAudio: true,
    source: 'telegram-file',
  };
}

export async function sendMessage(chatId, text, extra = {}) {
  const interactiveText = String(text);
  if (interactiveText.includes('TikTok photo/slideshow dikesan')) {
    await ensureInteractiveWebhook();
  }

  return telegram('sendMessage', {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
    ...extra,
  });
}

export function sendChatAction(chatId, action = 'typing') {
  return telegram('sendChatAction', { chat_id: chatId, action });
}

export function sendVideoUrl(chatId, url, caption = '', extra = {}) {
  return telegram('sendVideo', {
    chat_id: chatId,
    video: url,
    caption: caption.slice(0, 1024),
    supports_streaming: true,
    ...extra,
  });
}

export async function sendVideoUpload(chatId, item, caption = '', extra = {}) {
  if (!item?.url) throw new Error('Video source URL is missing.');
  await assertChatWriteAllowed('sendVideo', { chat_id: chatId });

  const limit = uploadLimitBytes();
  const knownSize = Number(item.filesize || 0);
  if (knownSize > limit) {
    const err = new Error(`Video is too large for the configured Telegram upload limit (${knownSize} bytes).`);
    err.code = 'TELEGRAM_FILE_TOO_LARGE';
    throw err;
  }

  const upstream = await fetch(item.url, {
    method: 'GET',
    headers: sourceHeaders(item.headers),
    redirect: 'follow',
    signal: AbortSignal.timeout(Number(process.env.MEDIA_FETCH_TIMEOUT_MS || 45000)),
  });

  if (!upstream.ok) {
    const err = new Error(`Media source returned HTTP ${upstream.status}.`);
    err.code = 'MEDIA_FETCH_ERROR';
    throw err;
  }

  const contentLength = Number(upstream.headers.get('content-length') || 0);
  if (contentLength > limit) {
    const err = new Error(`Video is too large for the configured Telegram upload limit (${contentLength} bytes).`);
    err.code = 'TELEGRAM_FILE_TOO_LARGE';
    throw err;
  }

  const arrayBuffer = await upstream.arrayBuffer();
  if (arrayBuffer.byteLength > limit) {
    const err = new Error(`Video is too large for the configured Telegram upload limit (${arrayBuffer.byteLength} bytes).`);
    err.code = 'TELEGRAM_FILE_TOO_LARGE';
    throw err;
  }

  const contentType = upstream.headers.get('content-type') || 'video/mp4';
  const extension = extensionFor(item, contentType);
  const buffer = Buffer.from(arrayBuffer);
  const probedMetadata = await probeTelegramVideoBuffer(buffer, extension).catch((error) => {
    console.warn('[telegram/video-probe] failed:', error?.message);
    return {};
  });
  const form = new FormData();
  form.set('chat_id', String(chatId));
  form.set('caption', caption.slice(0, 1024));
  form.set('supports_streaming', 'true');
  appendFormExtra(form, {
    ...extra,
    ...probedMetadata,
  });
  form.set('video', new Blob([buffer], { type: contentType }), `video.${extension}`);

  const response = await fetch(telegramEndpoint('sendVideo'), {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(Number(process.env.TELEGRAM_UPLOAD_TIMEOUT_MS || 55000)),
  });
  return parseTelegramResponse(response, 'sendVideo');
}

export async function sendVideoFileUpload(chatId, filePath, caption = '', extra = {}) {
  if (!filePath) throw new Error('Local video path is missing.');
  await assertChatWriteAllowed('sendVideo', { chat_id: chatId });

  const fileStat = await stat(filePath);
  const limit = uploadLimitBytes();
  if (fileStat.size > limit) {
    const err = new Error(`Video is too large for the configured Telegram upload limit (${fileStat.size} bytes).`);
    err.code = 'TELEGRAM_FILE_TOO_LARGE';
    throw err;
  }

  const buffer = await readFile(filePath);
  const extension = path.extname(filePath).replace(/^\./, '').toLowerCase() || 'mp4';
  const contentType = extension === 'webm' ? 'video/webm' : extension === 'mov' ? 'video/quicktime' : 'video/mp4';
  const probedMetadata = await probeTelegramVideoMetadata(filePath).catch(() => ({}));
  const form = new FormData();
  form.set('chat_id', String(chatId));
  form.set('caption', caption.slice(0, 1024));
  form.set('supports_streaming', 'true');
  appendFormExtra(form, {
    ...extra,
    ...probedMetadata,
  });
  form.set('video', new Blob([buffer], { type: contentType }), `video.${extension}`);

  const response = await fetch(telegramEndpoint('sendVideo'), {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(Number(process.env.TELEGRAM_UPLOAD_TIMEOUT_MS || 55000)),
  });
  return parseTelegramResponse(response, 'sendVideo');
}

export async function sendAudioFileUpload(chatId, filePath, fileName, caption = '', extra = {}) {
  if (!filePath) throw new Error('Local audio path is missing.');
  await assertChatWriteAllowed('sendAudio', { chat_id: chatId });

  const fileStat = await stat(filePath);
  const limit = uploadLimitBytes();
  if (fileStat.size > limit) {
    const err = new Error(`Audio is too large for the configured Telegram upload limit (${fileStat.size} bytes).`);
    err.code = 'TELEGRAM_FILE_TOO_LARGE';
    throw err;
  }

  const buffer = await readFile(filePath);
  const safeName = String(fileName || path.basename(filePath) || 'audio.mp3').slice(0, 180);
  const extension = path.extname(safeName).replace(/^\./, '').toLowerCase();
  const contentType = extension === 'm4a' ? 'audio/mp4' : extension === 'ogg' ? 'audio/ogg' : 'audio/mpeg';
  const form = new FormData();
  form.set('chat_id', String(chatId));
  if (caption) form.set('caption', String(caption).slice(0, 1024));
  appendFormExtra(form, extra);
  form.set('audio', new Blob([buffer], { type: contentType }), safeName);

  const response = await fetch(telegramEndpoint('sendAudio'), {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(Number(process.env.TELEGRAM_UPLOAD_TIMEOUT_MS || 55000)),
  });
  return parseTelegramResponse(response, 'sendAudio');
}

export function sendPhotoUrl(chatId, url, caption = '') {
  return telegram('sendPhoto', {
    chat_id: chatId,
    photo: url,
    caption: caption.slice(0, 1024),
  });
}

export function sendMediaGroup(chatId, items) {
  return telegram('sendMediaGroup', {
    chat_id: chatId,
    media: items.slice(0, 10),
  });
}

export function sendDownloadButton(chatId, text, url, label = '⬇️ Download') {
  return sendMessage(chatId, text, {
    reply_markup: {
      inline_keyboard: [[{ text: label, url }]],
    },
  });
}


/**
 * Read-only verification for tokens supplied by PayPing bot owners.
 * Only three Telegram Bot API methods are permitted. This never sends
 * messages or changes channel permissions/webhooks for external bots.
 */
export async function verifyExternalTelegramBotToken(rawToken){
  const token=String(rawToken||'').trim().slice(0,220);
  if(!/^\d{6,15}:[A-Za-z0-9_-]{20,}$/.test(token)){
    const e=new Error('Format Telegram Bot Token tidak sah.');e.code='INVALID_TELEGRAM_BOT_TOKEN';throw e;
  }
  async function readOnly(method,data={}){
    if(!['getMe','getFile','getUserProfilePhotos'].includes(method))throw new Error('Read-only method not allowed');
    const response=await fetch(DEFAULT_TELEGRAM_API_BASE+'/bot'+encodeURIComponent(token)+'/'+method,{
      method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data),
      signal:AbortSignal.timeout(12000),
    });
    const json=await response.json().catch(()=>null);
    if(!response.ok||!json?.ok){const e=new Error(json?.description||'Bot verification failed.');e.code='TELEGRAM_BOT_VERIFY_FAILED';throw e}
    return json.result;
  }
  const me=await readOnly('getMe');
  if(!me?.is_bot){const e=new Error('Token ini bukan Telegram bot.');e.code='NOT_A_TELEGRAM_BOT';throw e}
  let avatarDataUrl='';
  try{
    const photos=await readOnly('getUserProfilePhotos',{user_id:me.id,limit:1});
    const sizes=photos?.photos?.[0]||[],photo=sizes[sizes.length-1];
    if(photo?.file_id){
      const file=await readOnly('getFile',{file_id:photo.file_id});
      if(file?.file_path&&/^[A-Za-z0-9_./-]+$/.test(file.file_path)){
        const img=await fetch(DEFAULT_TELEGRAM_API_BASE+'/file/bot'+encodeURIComponent(token)+'/'+file.file_path,{signal:AbortSignal.timeout(12000)});
        const type=(img.headers.get('content-type')||'').toLowerCase().split(';')[0].trim();
        const len=Number(img.headers.get('content-length')||0);
        if(img.ok&&['image/jpeg','image/png'].includes(type)&&len<=1500000){
          const bytes=Buffer.from(await img.arrayBuffer());
          if(bytes.length<=1500000)avatarDataUrl='data:'+type+';base64,'+bytes.toString('base64');
        }
      }
    }
  }catch{/* Avatar is optional; bot token verification must still succeed. */}
  const name=String([me.first_name,me.last_name].filter(Boolean).join(' ')).trim().slice(0,120);
  return {token,id:String(me.id||''),username:String(me.username||'').replace(/^@+/,'').slice(0,80),
    name:name||String(me.username||'Bot').slice(0,120),
    canJoinGroups:Boolean(me.can_join_groups),supportsInlineQueries:Boolean(me.supports_inline_queries),
    avatarDataUrl};
}
