import { strict as assert } from 'node:assert';
import { mock } from 'node:test';
import { visiblePolling } from './visiblePolling.ts';

class Visibility extends EventTarget {
  hidden = false;
  change(hidden) { this.hidden = hidden; this.dispatchEvent(new Event('visibilitychange')); }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
mock.timers.enable({ apis: ['setTimeout'] });
try {
  const visibility = new Visibility();
  let calls = 0, release;
  const stop = visiblePolling(() => { calls++; return new Promise(resolve => { release = resolve; }); }, 100, visibility);
  assert.equal(calls, 1);
  mock.timers.tick(1000);
  visibility.change(true); visibility.change(false);
  assert.equal(calls, 1, 'visibility changes cannot overlap an in-flight request');
  release(); await flush();
  visibility.change(true);
  mock.timers.tick(1000); await flush();
  assert.equal(calls, 1, 'hidden windows do not poll');
  visibility.change(false);
  assert.equal(calls, 2, 'restoring refreshes immediately');
  stop(); release(); await flush(); mock.timers.tick(1000);
  visibility.change(true); visibility.change(false);
  assert.equal(calls, 2, 'disposing during a request prevents rescheduling');

  visibility.change(true);
  const stopHidden = visiblePolling(async () => { calls++; }, 100, visibility);
  assert.equal(calls, 2, 'initially hidden windows do not fetch');
  visibility.change(false); await flush();
  assert.equal(calls, 3);
  mock.timers.tick(100); await flush();
  assert.equal(calls, 4, 'visible polling continues at the requested interval');
  stopHidden();

  let failures = 0;
  const stopFailing = visiblePolling(async () => { failures++; throw Error('offline'); }, 100, visibility);
  await flush(); mock.timers.tick(100); await flush();
  assert.equal(failures, 2, 'transient failures do not stop future polling');
  stopFailing();
} finally { mock.timers.reset(); }
console.log('PASS visibility, resume, concurrency, disposal and failure recovery');
