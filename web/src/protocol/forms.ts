// The start form rules of src/winslow/ui/validation.py and
// workflow_form_widget.py: which control an OptionInfo gets, its initial
// value, and how the form values collect for create_session.

import type { OptionInfo } from './model'

export type RawValue = string | boolean | string[] | null

export type Control = 'switch' | 'checkboxes' | 'radio' | 'select' | 'text'

// If a single-select field has more choices than this, the form uses a
// select instead of a radio set.
export const MAX_OPTIONS_FOR_RADIO = 10

export interface FormValues {
  orchestrator: Record<string, unknown>
  workflow: Record<string, unknown>
}

export function controlOf(option: OptionInfo): Control {
  if (option.action === 'store_true' || option.action === 'store_false' || option.action === 'store_const') {
    return 'switch'
  }
  if (option.choices) {
    if (option.multiselect) return 'checkboxes'
    return option.choices.length > MAX_OPTIONS_FOR_RADIO ? 'select' : 'radio'
  }
  return 'text'
}

// The checkbox state an OptionInfo implies. A store_const value shows as
// checked when the parsed initial holds any value.
export function switchChecked(option: OptionInfo): boolean {
  if (option.action === 'store_const') return option.initial !== null
  return option.initial === 'True'
}

export function initialRaw(option: OptionInfo): RawValue {
  switch (controlOf(option)) {
    case 'switch':
      return switchChecked(option)
    case 'checkboxes':
      return [...(option.initial_selection ?? [])]
    case 'radio':
    case 'select':
      return option.initial ?? null
    case 'text':
      return option.initial ?? ''
  }
}

export function inputType(option: OptionInfo): 'number' | 'text' {
  return option.type === 'int' || option.type === 'float' || option.type === 'Decimal' ? 'number' : 'text'
}

export function labelOf(name: string): string {
  return name.replace(/_/g, '-')
}

// A scalar type parses inline for an immediate field error. Every other value
// stays a string for create_session to parse with the declared option type.
function convert(value: string, option: OptionInfo): unknown {
  switch (option.type) {
    case 'int': {
      if (!/^[+-]?\d+$/.test(value.trim())) throw new Error(`invalid literal for int: ${JSON.stringify(value)}`)
      return Number.parseInt(value, 10)
    }
    case 'float': {
      const parsed = Number(value.trim())
      if (value.trim() === '' || Number.isNaN(parsed)) {
        throw new Error(`could not convert string to float: ${JSON.stringify(value)}`)
      }
      return parsed
    }
    default:
      return value
  }
}

export function collect(option: OptionInfo, raw: RawValue): unknown {
  switch (controlOf(option)) {
    case 'switch': {
      const checked = raw === true
      if (option.action === 'store_true') return checked
      if (option.action === 'store_false') return !checked
      return checked ? option.const : null
    }
    case 'checkboxes':
      return (Array.isArray(raw) ? raw : []).map((item) => convert(item, option))
    case 'radio':
    case 'select':
      return typeof raw === 'string' && raw !== '' ? convert(raw, option) : null
    case 'text':
      return typeof raw === 'string' && raw !== '' ? convert(raw, option) : null
  }
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined || (Array.isArray(value) && value.length === 0)
}

export function validateGroup(
  options: readonly OptionInfo[],
  raws: Record<string, RawValue>,
): { values: Record<string, unknown>; errors: Record<string, string> } {
  const values: Record<string, unknown> = {}
  const errors: Record<string, string> = {}
  for (const option of options) {
    let value: unknown
    try {
      value = collect(option, raws[option.name] ?? null)
    } catch (error) {
      errors[option.name] = (error as Error).message
      continue
    }
    values[option.name] = value
    if (option.required && isMissing(value)) errors[option.name] = 'this field is required'
  }
  // A field with a value demands that its dependencies also have a value.
  for (const option of options) {
    const value = values[option.name]
    if (!option.depends_on.length || !value) continue
    for (const dependency of option.depends_on) {
      if (dependency in values && !values[dependency]) {
        errors[option.name] = `can only be used with ${labelOf(dependency)}`
      }
    }
  }
  return { values, errors }
}
