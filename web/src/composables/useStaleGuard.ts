// A generation counter for async handlers: take a token before the await,
// act only when it is still the latest one after it.

export function useStaleGuard() {
  let current = 0
  return {
    next: () => ++current,
    isCurrent: (token: number) => token === current,
  }
}
