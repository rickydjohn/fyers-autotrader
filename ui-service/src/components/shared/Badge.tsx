import { clsx } from 'clsx'

interface BadgeProps {
  label: string
  variant?: 'buy' | 'sell' | 'hold' | 'bullish' | 'bearish' | 'neutral'
  size?: 'sm' | 'md'
}

const variantStyles: Record<string, string> = {
  buy:     'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
  sell:    'bg-red-500/20 text-red-400 border border-red-500/30',
  hold:    'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30',
  bullish: 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
  bearish: 'bg-red-500/20 text-red-400 border border-red-500/30',
  neutral: 'bg-slate-500/20 text-slate-400 border border-slate-500/30',
}

export function Badge({ label, variant = 'neutral', size = 'sm' }: BadgeProps) {
  return (
    <span className={clsx(
      'inline-block rounded font-mono font-semibold uppercase tracking-wide',
      size === 'sm' ? 'px-1.5 py-0.5 text-xs' : 'px-2.5 py-1 text-sm',
      variantStyles[variant] ?? variantStyles.neutral
    )}>
      {label}
    </span>
  )
}
