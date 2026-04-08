import { format as dateFnsFormat } from 'date-fns'

/**
 * Parse an ISO timestamp that may have microsecond precision (6 decimal places).
 * JavaScript's Date constructor fails on >3 decimal places in some environments.
 * Truncates to milliseconds before parsing.
 */
export function parseDate(ts: string | null | undefined): Date {
  if (!ts) return new Date(NaN)
  return new Date(ts.replace(/(\.\d{3})\d+/, '$1'))
}

/**
 * Format a date string safely — returns fallback if the date is invalid
 * instead of throwing (date-fns format() throws on Invalid Date).
 */
export function safeFormat(ts: string | null | undefined, fmt: string, fallback = '--:--'): string {
  const d = parseDate(ts)
  if (isNaN(d.getTime())) return fallback
  return dateFnsFormat(d, fmt)
}
