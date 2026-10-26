import type { Tone } from './types.ts';

export function notificationLifetime(tone: Tone): number {
  return tone === 'error' || tone === 'warning' ? 7000 : 4500;
}
