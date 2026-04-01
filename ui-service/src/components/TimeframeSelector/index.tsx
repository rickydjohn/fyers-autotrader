import type { Timeframe } from '../../types'

const TIMEFRAMES: { value: Timeframe; label: string }[] = [
  { value: '1m',    label: '1m' },
  { value: '5m',    label: '5m' },
  { value: '15m',   label: '15m' },
  { value: '1h',    label: '1h' },
  { value: 'daily', label: 'D' },
]

interface Props {
  selected: Timeframe
  onChange: (tf: Timeframe) => void
}

export function TimeframeSelector({ selected, onChange }: Props) {
  return (
    <div className="flex gap-1">
      {TIMEFRAMES.map(({ value, label }) => (
        <button
          key={value}
          onClick={() => onChange(value)}
          className={`px-2.5 py-1 rounded text-xs font-mono transition-colors ${
            selected === value
              ? 'bg-blue-600 text-white'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}
