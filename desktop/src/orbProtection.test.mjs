import { strict as assert } from 'node:assert';
import { mock } from 'node:test';
import { OrbProtection } from './orbProtection.ts';
mock.timers.enable({apis:['setTimeout']});
try {
  let state, updates=0;
  const control=new OrbProtection(value=>{state=value;updates++;});
  control.idle(true,false);
  mock.timers.tick(19_999); assert.equal(state.dim,false);
  mock.timers.tick(1); assert.equal(state.dim,true);
  mock.timers.tick(40_000); assert.ok(state.x!==0 || state.y!==0);
  const visited=new Set();
  for(let i=0;i<49;i++){
    mock.timers.tick(60_000); visited.add(`${state.x},${state.y}`);
    assert.ok(Math.abs(state.x)<=12&&Math.abs(state.y)<=12,'stay within native compact region');
  }
  assert.equal(visited.size,49);
  const last={...state}; control.idle(false);
  assert.equal(state.dim,false); assert.equal(state.x,last.x); assert.equal(state.y,last.y,'hover must not move the click target');
  const paused=updates; mock.timers.tick(180_000); assert.equal(updates,paused);
  control.idle(true,true,false);
  mock.timers.tick(20_000); assert.equal(state.dim,true,'stationary cursor does not defeat dimming');
  const stationary={...state}; mock.timers.tick(280_000);
  assert.equal(state.asleep,true); assert.equal(state.x,stationary.x); assert.equal(state.y,stationary.y);
  const sleeping=updates; mock.timers.tick(180_000); assert.equal(updates,sleeping,'sleep stops all timers');
  control.wake(); assert.equal(state.asleep,false); assert.equal(state.dim,false);
  mock.timers.tick(20_000); assert.equal(state.dim,true);
  control.idle(true); control.dispose(); const disposed=updates;
  mock.timers.tick(180_000); assert.equal(updates,disposed);
} finally {mock.timers.reset();}
console.log('PASS idle dimming, bounded pixel shift, hover pause and timer cleanup');
