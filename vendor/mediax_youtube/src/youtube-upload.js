import { execFile } from 'node:child_process';
import { chmod, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import ffmpegPath from 'ffmpeg-static';

const execFileAsync = promisify(execFile);

const QUALITY_PROFILES = [
  {
    label: '1080p',
    maxDimension: 1920,
    selector: 'bestvideo[ext=mp4][height<=1080][height>720]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080][height>720]',
  },
  {
    label: '720p',
    maxDimension: 1280,
    selector: 'bestvideo[ext=mp4][height<=720][height>480]+bestaudio[ext=m4a]/best[ext=mp4][height<=720][height>480]',
  },
  {
    label: '480p',
    maxDimension: 854,
    selector: 'bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/best[ext=mp4][height<=480]',
  },
];

function ytdlpBinary() {
  return path.join(process.cwd(), 'bin', 'yt-dlp');
}

function commonArgs() {
  return [
    '--no-playlist',
    '--no-warnings',
    '--no-check-certificates',
    '--js-runtimes', `node:${process.execPath}`,
    '--remote-components', 'ejs:github',
  ];
}

function commandOptions(timeoutMs) {
  return {
    timeout: timeoutMs,
    maxBuffer: 12 * 1024 * 1024,
    env: {
      ...process.env,
      PATH: `${path.dirname(process.execPath)}:${process.env.PATH || ''}`,
    },
  };
}

function selectedSize(info = {}) {
  const requested = Array.isArray(info.requested_formats) ? info.requested_formats : [];
  if (requested.length) {
    let total = 0;
    for (const format of requested) {
      const size = Number(format?.filesize || format?.filesize_approx || 0);
      if (!size) return 0;
      total += size;
    }
    return total;
  }
  return Number(info.filesize || info.filesize_approx || 0);
}

async function probe(url, profile) {
  const binary = ytdlpBinary();
  await chmod(binary, 0o755).catch(() => {});
  const args = [
    ...commonArgs(),
    '--dump-single-json',
    '--skip-download',
    '--format', profile.selector,
    '--',
    url,
  ];

  try {
    const { stdout } = await execFileAsync(
      binary,
      args,
      commandOptions(Number(process.env.YOUTUBE_PROBE_TIMEOUT_MS || 12000)),
    );
    const info = JSON.parse(stdout);
    return {
      info,
      estimatedSize: selectedSize(info),
      duration: Number(info?.duration || 0),
      title: String(info?.title || ''),
    };
  } catch (error) {
    const detail = String(error?.stderr || error?.stdout || error?.message || 'yt-dlp probe failed');
    if (/Requested format is not available|format is not available|No video formats found/i.test(detail)) {
      return null;
    }
    throw error;
  }
}

async function download(url, profile, outputBase) {
  const binary = ytdlpBinary();
  const outputTemplate = `${outputBase}.%(ext)s`;
  const args = [
    ...commonArgs(),
    '--format', profile.selector,
    '--merge-output-format', 'mp4',
    '--ffmpeg-location', ffmpegPath,
    '--no-progress',
    '--output', outputTemplate,
    '--print', 'after_move:filepath',
    '--',
    url,
  ];

  const { stdout } = await execFileAsync(
    binary,
    args,
    commandOptions(Number(process.env.YOUTUBE_DOWNLOAD_TIMEOUT_MS || 35000)),
  );

  const lines = String(stdout || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const reported = lines.at(-1);
  const candidates = [reported, `${outputBase}.mp4`, `${outputBase}.mkv`, `${outputBase}.webm`].filter(Boolean);

  for (const candidate of candidates) {
    try {
      const fileStat = await stat(candidate);
      if (fileStat.isFile()) return { filePath: candidate, size: fileStat.size };
    } catch {}
  }

  const err = new Error('yt-dlp completed but the merged output file was not found.');
  err.code = 'YOUTUBE_OUTPUT_MISSING';
  throw err;
}

function compressionPlan(durationSeconds, maxBytes, sourceProfile) {
  const duration = Number(durationSeconds || 0);
  if (!Number.isFinite(duration) || duration <= 0) return null;

  // Leave headroom for MP4 container overhead and bitrate variance.
  const targetBytes = Math.floor(maxBytes * 0.90);
  const audioKbps = duration > 10 * 60 ? 96 : 128;
  const totalKbps = Math.floor((targetBytes * 8) / duration / 1000);
  const videoKbps = totalKbps - audioKbps - 24;

  // Below this point it is no longer reasonable to call the result HQ.
  if (!Number.isFinite(videoKbps) || videoKbps < 350) return null;

  let desiredDimension;
  let label;
  if (videoKbps >= 2200) {
    desiredDimension = 1920;
    label = '1080p';
  } else if (videoKbps >= 1000) {
    desiredDimension = 1280;
    label = '720p';
  } else {
    desiredDimension = 854;
    label = '480p';
  }

  const maxDimension = Math.min(sourceProfile.maxDimension, desiredDimension);
  if (maxDimension <= 854) label = '480p';
  else if (maxDimension <= 1280) label = '720p';
  else label = '1080p';

  return {
    targetBytes,
    audioKbps,
    videoKbps,
    maxDimension,
    label,
  };
}

async function compressForTelegram(inputPath, outputPath, plan) {
  await rm(outputPath, { force: true }).catch(() => {});

  const maxRate = Math.max(plan.videoKbps, Math.floor(plan.videoKbps * 1.08));
  const bufferSize = Math.max(plan.videoKbps * 2, 700);
  const scale = `scale=${plan.maxDimension}:${plan.maxDimension}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos`;

  const args = [
    '-y',
    '-i', inputPath,
    '-map', '0:v:0',
    '-map', '0:a:0?',
    '-vf', scale,
    '-c:v', 'libx264',
    '-preset', String(process.env.YOUTUBE_COMPRESS_PRESET || 'veryfast'),
    '-profile:v', 'high',
    '-pix_fmt', 'yuv420p',
    '-b:v', `${plan.videoKbps}k`,
    '-maxrate', `${maxRate}k`,
    '-bufsize', `${bufferSize}k`,
    '-c:a', 'aac',
    '-b:a', `${plan.audioKbps}k`,
    '-ac', '2',
    '-movflags', '+faststart',
    '-map_metadata', '-1',
    outputPath,
  ];

  await execFileAsync(
    ffmpegPath,
    args,
    commandOptions(Number(process.env.YOUTUBE_COMPRESS_TIMEOUT_MS || 45000)),
  );

  const fileStat = await stat(outputPath);
  if (!fileStat.isFile()) {
    const err = new Error('FFmpeg compression completed but no output file was created.');
    err.code = 'YOUTUBE_COMPRESS_OUTPUT_MISSING';
    throw err;
  }

  return { filePath: outputPath, size: fileStat.size };
}

async function cleanupOutputBase(outputBase) {
  const suffixes = [
    '.mp4', '.mkv', '.webm', '.m4a', '.part',
    '-compressed.mp4',
  ];
  await Promise.all(suffixes.map((suffix) => rm(`${outputBase}${suffix}`, { force: true }).catch(() => {})));
}

export async function prepareYouTubeTelegramUpload(url, maxBytes) {
  const limit = Number(maxBytes || 0);
  if (!Number.isFinite(limit) || limit <= 0) throw new Error('A valid Telegram upload limit is required.');

  const attemptId = randomUUID();
  const outputBase = path.join(tmpdir(), `ar-youtube-${attemptId}`);
  const compressedPath = `${outputBase}-compressed.mp4`;
  let lastError = null;

  for (const profile of QUALITY_PROFILES) {
    let probeResult;
    try {
      probeResult = await probe(url, profile);
    } catch (error) {
      lastError = error;
      continue;
    }
    if (!probeResult) continue;

    const expectedCompression = probeResult.estimatedSize > limit
      ? compressionPlan(probeResult.duration, limit, profile)
      : null;

    if (probeResult.estimatedSize > limit && !expectedCompression) {
      console.info(`YouTube ${profile.label} is over the Telegram limit and cannot be compressed to HQ within the limit.`);
      continue;
    }

    try {
      const result = await download(url, profile, outputBase);

      if (result.size <= limit) {
        return {
          ...result,
          quality: profile.label,
          title: probeResult.title,
          compressed: false,
          cleanup: async () => cleanupOutputBase(outputBase),
        };
      }

      const plan = compressionPlan(probeResult.duration, limit, profile);
      if (!plan) {
        console.info(`YouTube ${profile.label} downloaded at ${result.size} bytes but HQ compression is not viable.`);
        await rm(result.filePath, { force: true }).catch(() => {});
        continue;
      }

      console.info(
        `Compressing YouTube ${profile.label}: ${result.size} bytes -> target <= ${plan.targetBytes} bytes, ` +
        `${plan.videoKbps}k video + ${plan.audioKbps}k audio, maxDimension=${plan.maxDimension}.`,
      );

      const compressed = await compressForTelegram(result.filePath, compressedPath, plan);
      await rm(result.filePath, { force: true }).catch(() => {});

      if (compressed.size > limit) {
        console.info(`Compressed YouTube output still exceeds Telegram limit: ${compressed.size} > ${limit}.`);
        await rm(compressed.filePath, { force: true }).catch(() => {});
        continue;
      }

      return {
        ...compressed,
        quality: `${plan.label} • compressed HQ`,
        title: probeResult.title,
        compressed: true,
        cleanup: async () => cleanupOutputBase(outputBase),
      };
    } catch (error) {
      lastError = error;
      await cleanupOutputBase(outputBase);
    }
  }

  const err = new Error(lastError?.message || 'No YouTube version can be delivered within the Telegram upload limit at acceptable quality.');
  err.code = 'YOUTUBE_NO_SENDABLE_QUALITY';
  throw err;
}
