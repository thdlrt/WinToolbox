import test from 'node:test';
import assert from 'node:assert/strict';
import { OrbMotion } from '../desktop/src/orbMotion.ts';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const point = { x: 64, y: 160 };

test('close animation happens before compact native clipping', async () => {
  const events = [];
  const motion = new OrbMotion(async on => { events.push(['native', on]); return point; }, on => events.push(['render', on]), () => 30, assert.fail);
  await motion.show(); events.length = 0; motion.hide();
  assert.deepEqual(events, [['render', false]]);
  await wait(50); assert.deepEqual(events, [['render', false], ['native', false], ['render', false]]); motion.dispose();
});
test('rapid reopen cancels stale closing clip', async () => {
  const native = [];
  const motion = new OrbMotion(async on => { native.push(on); return point; }, () => {}, () => 30, assert.fail);
  await motion.show(); motion.hide(); await motion.show(); await wait(50);
  assert.deepEqual(native, [true, true]); motion.dispose();
});
test('late native completion cannot paint stale state and calls are serialized', async () => {
  let resolveFirst; const native = [], paints = [];
  const motion = new OrbMotion(on => { native.push(on); return native.length === 1 ? new Promise(resolve => { resolveFirst = resolve; }) : Promise.resolve(point); }, on => paints.push(on), () => 0, assert.fail);
  void motion.show(); await wait(0); motion.hide(); await wait(5);
  assert.deepEqual(native, [true]); resolveFirst(point); await wait(5);
  assert.deepEqual(native, [true, false]); assert.deepEqual(paints, [false, false]); motion.dispose();
});
test('unmount cancels pending clipping', async () => {
  const native = [];
  const motion = new OrbMotion(async on => { native.push(on); return point; }, () => {}, () => 15, assert.fail);
  motion.hide(); motion.dispose(); await wait(30); assert.deepEqual(native, []);
});
