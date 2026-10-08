import { describe, expect, it } from 'vitest'

import router, { recordLocation, sessionLocation } from '@/router'

describe('the record route', () => {
  it('names one record as a plain path, with the info tail on demand', () => {
    const record = router.resolve(
      recordLocation('etl-20260911T100000-1a2b3c4d', 'a1b2c3d4-uuid', 'load-cities-3f9a1c2e'),
    )
    expect(record.path).toBe('/sessions/etl-20260911T100000-1a2b3c4d/records/a1b2c3d4-uuid/load-cities-3f9a1c2e')
    const info = router.resolve(recordLocation('s', 'b', 'k', true))
    expect(info.path).toBe('/sessions/s/records/b/k/info')
  })

  it('parses the path back into the view props', () => {
    const parsed = router.resolve('/sessions/s/records/b/k/info')
    expect(parsed.name).toBe('record')
    expect(parsed.params).toEqual({ id: 's', batch: 'b', task: 'k', view: 'info' })
    expect(router.resolve('/sessions/s/records/b/k/other').name).not.toBe('record')
    expect(router.resolve(sessionLocation('s')).path).toBe('/sessions/s')
  })
})
