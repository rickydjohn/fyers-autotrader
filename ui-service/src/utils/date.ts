/**
 * Parse an ISO timestamp that may have microsecond precision (6 decimal places).
 * JavaScript's Date constructor fails on >3 decimal places in some environments.
 * Truncates to milliseconds before parsing.
 */
export function parseDate(ts: string | null | undefined): Date {
  if (!ts) return new Date(NaN)
  return new Date(ts.replace(/(\.\d{3})\d+/, '$1'))
}
