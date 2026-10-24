// The workflow list of the dashboard: the descriptors as a tree of nested workflows.

import type { WorkflowDescriptor } from '@/protocol/model'

export interface WorkflowRow {
  name: string
  // The nest level: 0 for a top-level workflow, 1 for its child.
  depth: number
}

// Each parent comes before its children. The siblings come in name order.
export function workflowRows(workflows: readonly WorkflowDescriptor[]): WorkflowRow[] {
  const childrenOf = (parent: string | null): string[] =>
    workflows
      .filter((row) => (row.parent ?? null) === parent)
      .map((row) => row.workflow)
      .sort()
  const below = (parent: string | null, depth: number): WorkflowRow[] =>
    childrenOf(parent).flatMap((name) => [{ name, depth }, ...below(name, depth + 1)])
  return below(null, 0)
}
