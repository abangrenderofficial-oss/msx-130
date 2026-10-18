export function positiveIntEnv(name, fallback, { min = 1, max = 2_147_483_647 } = {}) {
  const fallbackNumber = Number(fallback);
  const safeFallback = Number.isFinite(fallbackNumber) && fallbackNumber >= min
    ? Math.min(max, Math.floor(fallbackNumber))
    : min;

  const raw = String(process.env[name] ?? '').trim();
  if (!raw) return safeFallback;

  const value = Number(raw);
  if (!Number.isFinite(value) || value < min) return safeFallback;
  return Math.min(max, Math.floor(value));
}
