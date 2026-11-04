import type { RunEvent } from './api'

/** Merge only persisted, correctly correlated events. Event id and sequence both dedupe replays. */
export function mergeRunEvents(existing: RunEvent[], incoming: RunEvent[], runId: string): RunEvent[] {
  const byId = new Map<string, RunEvent>()
  const usedSeq = new Set<number>()
  for (const event of [...existing, ...incoming]) {
    if (!event || event.run_id !== runId || !event.event_id || !Number.isInteger(event.seq) || event.seq < 1) continue
    if (byId.has(event.event_id) || usedSeq.has(event.seq)) continue
    byId.set(event.event_id, event)
    usedSeq.add(event.seq)
  }
  return [...byId.values()].sort((left, right) => left.seq - right.seq)
}
