import { describe, expect, it } from 'vitest'

import { formatElapsed, formatSize, safeRepr } from '@/protocol/format'
import { CHECKABLE_STATUSES, RUNNABLE_STATUSES, taskIcon, taskVariant } from '@/protocol/status'

describe('the formatting', () => {
  it('renders an elapsed duration like the TUI', () => {
    expect(formatElapsed(12)).toBe('12s')
    expect(formatElapsed(185)).toBe('3m 05s')
    expect(formatElapsed(3840)).toBe('1h 04m')
  })

  it('renders a byte size like winslow.util.human_readable_size', () => {
    expect(formatSize(512)).toBe('512.00 B')
    expect(formatSize(16656252928)).toBe('16.66 GB')
    expect(formatSize(38654705664, 1)).toBe('38.7 GB')
  })

  it('renders a one-line safe repr', () => {
    expect(safeRepr('  a  b\n c ')).toBe('a b c')
    expect(safeRepr('')).toBe('""')
    expect(safeRepr({ a: 1 })).toBe('{"a":1}')
    expect(safeRepr('x'.repeat(120)).length).toBe(100)
  })
})

describe('the status tables', () => {
  it('mirror the Python sets', () => {
    expect(RUNNABLE_STATUSES.has('STALE')).toBe(true)
    expect(CHECKABLE_STATUSES.has('COMPLETED')).toBe(true)
    expect(RUNNABLE_STATUSES.has('COMPLETED')).toBe(false)
    expect(taskIcon('COMPLETED')).toBe('🟢')
    expect(taskVariant('BLOCKED')).toBe('danger')
  })
})
