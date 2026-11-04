import assert from 'node:assert/strict'
import test from 'node:test'
import { buildRunQuery, formatMetric, formatMoney, formatPercent, formatRunDuration, pageTrend, trendBarHeight, retrievalStrategyLabel, type AdminRunRow } from '../src/adminObservability.ts'

const row = (run_id: string, created_at: string, status: string): AdminRunRow => ({
  run_id, status, strategy: null, orchestrator: 'legacy', orchestrator_evidence: 'fixture', model_id: null, latency_ms: null, wall_duration_ms: null, latency_semantics: 'fixture',
  error_code: null, created_at, finished_at: null,
})

test('admin run query serializes supported filters and pagination', () => {
  const query = new URLSearchParams(buildRunQuery('2026-10-24T00:00:00Z', '2026-10-25T00:00:00Z', 3, 25, {
    status: 'UNKNOWN_RESULT', strategy: 'AGENT', agent: 'policy', toolId: 'search_knowledge', orchestrator: 'langgraph',
  }))
  assert.equal(query.get('offset'), '50')
  assert.equal(query.get('status'), 'UNKNOWN_RESULT')
  assert.equal(query.get('strategy'), 'AGENT')
  assert.equal(query.get('agent'), 'policy')
  assert.equal(query.get('tool_id'), 'search_knowledge')
  assert.equal(query.get('orchestrator'), 'langgraph')
})

test('unavailable metrics stay visibly unavailable rather than becoming zero', () => {
  assert.equal(formatMetric(null), '暂无数据')
  assert.equal(formatPercent(null), '暂无数据')
  assert.equal(formatMoney(null), '暂无数据')
  assert.equal(formatMetric(0), '0')
})

test('run duration keeps sub-second values in integer milliseconds and retrieval labels distinguish RRF from rerank', () => {
  assert.equal(formatRunDuration(0), '0 毫秒')
  assert.equal(formatRunDuration(33), '33 毫秒')
  assert.equal(formatRunDuration(999.7), '1000 毫秒')
  assert.equal(formatRunDuration(1000), '1.0 秒')
  assert.equal(formatRunDuration(null), '暂无数据')
  assert.equal(retrievalStrategyLabel('RRF'), 'RRF（融合）')
  assert.equal(retrievalStrategyLabel('HYBRID_RERANK'), 'HYBRID_RERANK（融合+重排）')
})

test('trend is explicitly based on bounded page rows and includes unknown as non-success', () => {
  const rows = [
    row('a', '2026-10-24T01:00:00Z', 'SUCCEEDED'),
    row('b', '2026-10-24T03:00:00Z', 'UNKNOWN_RESULT'),
    row('c', '2026-10-24T05:00:00Z', 'FAILED'),
  ]
  const result = pageTrend(rows, '2026-10-24T00:00:00Z', '2026-10-24T07:00:00Z', 7)
  assert.equal(result.length, 7)
  assert.equal(result.reduce((sum, bucket) => sum + bucket.count, 0), 3)
  assert.equal(result.reduce((sum, bucket) => sum + bucket.succeeded, 0), 1)
  assert.equal(result.reduce((sum, bucket) => sum + bucket.failed, 0), 2)
  assert.deepEqual(pageTrend(rows, 'invalid', '2026-10-24T07:00:00Z'), [])
})

test('24h buckets use unique timestamps and time labels, zero is zero height',()=>{
 const points=pageTrend([row('x','2026-11-01T03:00:00Z','SUCCEEDED')], '2026-11-01T00:00:00Z','2026-11-02T00:00:00Z')
 assert.equal(new Set(points.map(p=>p.key)).size,7)
 assert.equal(new Set(points.map(p=>p.label)).size,7)
 assert.equal(points[0].start,'2026-11-01T00:00:00.000Z')
 assert.equal(points.at(-1)?.end,'2026-11-02T00:00:00.000Z')
 assert.equal(points.reduce((n,p)=>n+p.count,0),1)
 assert.equal(trendBarHeight(0,3),'0%')
 assert.equal(trendBarHeight(0,0),'0%')
 assert.equal(trendBarHeight(1,2),'50%')
 assert.deepEqual(pageTrend([], '2026-11-01T00:00:00Z','2026-11-02T00:00:00Z'),[])
})
