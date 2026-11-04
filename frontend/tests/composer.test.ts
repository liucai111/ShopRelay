import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isSendShortcut } from '../src/composer.ts'
test('Chinese composition never submits; modifier Enter does', () => {
  const event = { key: 'Enter', ctrlKey: true, metaKey: false, isComposing: false, keyCode: 13 }
  assert.equal(isSendShortcut(event), true)
  assert.equal(isSendShortcut({ ...event, isComposing: true }), false)
  assert.equal(isSendShortcut({ ...event, keyCode: 229 }), false)
  assert.equal(isSendShortcut({ ...event, ctrlKey: false }), false)
})
