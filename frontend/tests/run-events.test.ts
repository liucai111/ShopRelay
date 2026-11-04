import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeRunEvents } from '../src/runEvents.ts'
import type { RunEvent } from '../src/api.ts'

const event = (eventId: string, runId: string, seq: number): RunEvent => ({
  event_id: eventId, run_id: runId, session_id: 's1', trace_id: 't1', seq,
  timestamp: new Date(seq * 1000).toISOString(), type: 'started', payload: {},
})

test('mergeRunEvents sorts, deduplicates by event id and sequence, and rejects foreign or incomplete events', () => {
  const merged = mergeRunEvents([event('e2', 'r1', 2)], [
    event('e1', 'r1', 1), event('e2', 'r1', 2), event('other', 'r2', 3),
    event('duplicate-seq', 'r1', 2), { ...event('missing-id', 'r1', 4), event_id: '' },
  ], 'r1')
  assert.deepEqual(merged.map(item => item.event_id), ['e1', 'e2'])
})
