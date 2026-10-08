import { describe, expect, it } from 'vitest'

import { collect, controlOf, initialRaw, validateGroup } from '@/protocol/forms'
import type { OptionInfo } from '@/protocol/model'

function option(overrides: Partial<OptionInfo>): OptionInfo {
  return {
    name: 'opt',
    help: null,
    default: null,
    initial: null,
    required: false,
    choices: null,
    multiselect: false,
    type: null,
    identifier: false,
    depends_on: [],
    action: null,
    const: null,
    initial_selection: null,
    ...overrides,
  }
}

describe('the control choice', () => {
  it('follows the action, the choices and their count', () => {
    expect(controlOf(option({ action: 'store_true' }))).toBe('switch')
    expect(controlOf(option({ choices: ['a', 'b'] }))).toBe('radio')
    expect(controlOf(option({ choices: ['a', 'b'], multiselect: true }))).toBe('checkboxes')
    expect(controlOf(option({ choices: Array.from({ length: 11 }, (_, i) => String(i)) }))).toBe('select')
    expect(controlOf(option({ type: 'int' }))).toBe('text')
  })

  it('prefills from the parsed initial value', () => {
    expect(initialRaw(option({ action: 'store_true', initial: 'True' }))).toBe(true)
    expect(initialRaw(option({ action: 'store_const', initial: 'x', const: 'x' }))).toBe(true)
    expect(initialRaw(option({ choices: ['a', 'b'], multiselect: true, initial_selection: ['b'] }))).toEqual(['b'])
    expect(initialRaw(option({ type: 'int', initial: '3' }))).toBe('3')
  })
})

describe('the collection', () => {
  it('parses the scalar types inline and keeps the rest as strings', () => {
    expect(collect(option({ type: 'int' }), '42')).toBe(42)
    expect(collect(option({ type: 'float' }), '1.5')).toBe(1.5)
    expect(collect(option({ type: 'Decimal' }), '1.50')).toBe('1.50')
    expect(collect(option({ type: 'int' }), '')).toBeNull()
    expect(() => collect(option({ type: 'int' }), 'x')).toThrow(/invalid literal/)
  })

  it('applies the switch semantics', () => {
    expect(collect(option({ action: 'store_false' }), true)).toBe(false)
    expect(collect(option({ action: 'store_const', const: 7 }), true)).toBe(7)
    expect(collect(option({ action: 'store_const', const: 7 }), false)).toBeNull()
  })
})

describe('the validation', () => {
  it('marks required and dependent fields', () => {
    const options = [option({ name: 'name', required: true }), option({ name: 'suffix', depends_on: ['name'] })]
    const { values, errors } = validateGroup(options, { name: '', suffix: 'x' })
    expect(values).toEqual({ name: null, suffix: 'x' })
    expect(errors).toEqual({ name: 'this field is required', suffix: 'can only be used with name' })
  })
})
