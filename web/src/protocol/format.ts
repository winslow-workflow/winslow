// The display helpers of src/winslow/ui/formatting.py and winslow.util.safe_repr.

export function formatElapsed(seconds: number): string {
  const total = Math.floor(seconds)
  if (total < 60) return `${total}s`
  if (total < 3600) return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`
  return `${Math.floor(total / 3600)}h ${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}m`
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

export function formatTimestamp(epoch: number): string {
  const d = new Date(epoch * 1000)
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatClock(epoch: number): string {
  const d = new Date(epoch * 1000)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatVerification(checkedAt: number | null, effectiveTtl: number | null): string | null {
  if (checkedAt === null) return null
  const stamp = formatTimestamp(checkedAt)
  return effectiveTtl === null ? stamp : `${stamp} (ttl ${formatElapsed(effectiveTtl)})`
}

export function formatDuration(seconds: number): string {
  return `${seconds.toFixed(1)}s`
}

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

// The byte size of winslow.util.human_readable_size: base 1000, PB at most.
export function formatSize(size: number, decimalPlaces = 2): string {
  let value = size
  let index = 0
  while (value >= 1000 && index < SIZE_UNITS.length - 1) {
    value /= 1000
    index += 1
  }
  return `${value.toFixed(decimalPlaces)} ${SIZE_UNITS[index]}`
}

export function shortUuid(uuid: string): string {
  return uuid.slice(0, 8)
}

// One-line display form: collapsed whitespace, limited length. A string
// renders without quotes unless it would render as nothing.
export function safeRepr(value: unknown, limit = 100): string {
  let text: string
  if (typeof value === 'string') {
    text = value.split(/\s+/).filter(Boolean).join(' ')
    if (!text) text = JSON.stringify(value)
  } else if (value === null || value === undefined) {
    text = 'None'
  } else if (typeof value === 'boolean') {
    text = value ? 'True' : 'False'
  } else if (typeof value === 'number') {
    text = String(value)
  } else {
    try {
      text = JSON.stringify(value) ?? String(value)
    } catch (error) {
      text = `<unrepresentable: ${(error as Error).name}: ${(error as Error).message}>`
    }
    text = text.split(/\s+/).filter(Boolean).join(' ')
  }
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

export function plural(count: number, noun: string, pluralNoun = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : pluralNoun}`
}
