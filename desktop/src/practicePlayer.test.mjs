import { strict as assert } from 'node:assert';
import { PracticePlayer, practiceTokens } from './practicePlayer.ts';

class FakeAudio {
  src = ''; currentTime = 0; duration = 5; playbackRate = 1; paused = true; ended = false; playCalls = 0; pauseCalls = 0;
  listeners = new Map();
  addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(fn); }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  emit(name) { for (const fn of this.listeners.get(name) || []) fn({ type: name }); }
  play() { this.playCalls++; this.paused = false; this.ended = false; this.emit('play'); return Promise.resolve(); }
  pause() { this.pauseCalls++; this.paused = true; this.emit('pause'); }
  removeAttribute(name) { if (name === 'src') this.src = ''; }
  load() {}
  finish() { this.paused = true; this.ended = true; this.currentTime = this.duration; this.emit('ended'); }
}
const item = { id: 'item', text: 'First. Second.', voice: 'Cherry', model: 'test', created_at: 1, sentences: [{ text: 'First.', audio_id: 'a', duration: 5 }, { text: 'Second.', audio_id: 'b', duration: 4 }] };
const payload = id => ({ data: id, mime: 'audio/wav' });
const settle = () => new Promise(resolve => setImmediate(resolve));
function setup(fetch = async id => payload(id)) {
  const audio = new FakeAudio(); const states = []; const errors = []; const created = []; const revoked = [];
  const player = new PracticePlayer(audio, fetch, state => states.push(state), error => errors.push(error), value => { const url = `blob:${value.data}`; created.push(url); return url; }, url => revoked.push(url));
  player.setItem(item);
  return { player, audio, states, errors, created, revoked };
}

let reads = 0;
const first = setup(async id => { reads++; return payload(id); });
await first.player.play(0);
assert.equal(first.audio.src, 'blob:a');
assert.equal(first.states.at(-1).playing, true);
assert.equal(first.states.at(-1).index, 0);
first.audio.currentTime = 2;
first.player.pause();
assert.equal(first.audio.paused, true);
await first.player.play();
assert.equal(first.audio.currentTime, 2, 'resume must retain the current sentence position');
assert.equal(reads, 1, 'resume reuses the current Blob instead of rereading audio');
first.player.setRate(1.25);
assert.equal(first.audio.playbackRate, 1.25, 'speed changes apply to audio already playing');
first.player.setMode('repeat'); first.audio.finish(); await settle();
assert.equal(first.states.at(-1).index, 0, 'repeat stays on the current sentence');
assert.equal(first.audio.currentTime, 0);
assert.equal(reads, 1, 'loop reuses the existing local audio');
first.player.setMode('continuous'); first.audio.finish(); await settle();
assert.equal(first.states.at(-1).index, 1, 'switching mode while playing affects the next ending');
assert.equal(first.audio.src, 'blob:b');
assert.equal(first.audio.playbackRate, 1.25, 'speed persists across sentence switches');
assert.deepEqual(first.revoked, ['blob:a'], 'previous audio is revoked after replacement');
const playCount = first.audio.playCalls;
first.audio.finish(); await settle();
assert.equal(first.audio.playCalls, playCount, 'continuous mode stops after the last sentence');
assert.equal(first.states.at(-1).playing, false);
first.player.seek(100); assert.equal(first.audio.currentTime, 5, 'seek clamps to audio duration');
first.player.seek(-10); assert.equal(first.audio.currentTime, 0);
first.player.dispose();
assert.equal(first.audio.paused, true);
assert.equal(first.audio.src, '');
assert.deepEqual(first.revoked, ['blob:a', 'blob:b']);
assert.equal([...first.audio.listeners.values()].reduce((sum, set) => sum + set.size, 0), 0, 'dispose detaches audio events');

const pending = new Map();
const race = setup(id => new Promise(resolve => pending.set(id, resolve)));
const oldRequest = race.player.play(0);
const newRequest = race.player.play(1);
pending.get('b')(payload('b')); await newRequest;
pending.get('a')(payload('a')); await oldRequest;
assert.equal(race.audio.src, 'blob:b', 'late audio must not replace the newly selected sentence');
assert.deepEqual(race.created, ['blob:b'], 'stale audio responses never allocate Blob URLs');
assert.equal(race.audio.playCalls, 1);
assert.equal(race.states.at(-1).index, 1);
race.player.dispose();

let resolvePaused;
const paused = setup(() => new Promise(resolve => { resolvePaused = resolve; }));
const pausedRequest = paused.player.play(0); paused.player.pause();
resolvePaused(payload('a')); await pausedRequest;
assert.equal(paused.audio.playCalls, 0, 'pausing during a load cancels later autoplay');
assert.equal(paused.states.at(-1).loading, false);
assert.deepEqual(paused.created, []);
paused.player.dispose();

let resolveDisposed;
const disposed = setup(() => new Promise(resolve => { resolveDisposed = resolve; }));
const disposedRequest = disposed.player.play(0); disposed.player.dispose();
resolveDisposed(payload('a')); await disposedRequest;
assert.equal(disposed.audio.playCalls, 0, 'unmounting during a load cannot start background playback');
assert.deepEqual(disposed.created, []);

let resolveSwitched;
const switched = setup(() => new Promise(resolve => { resolveSwitched = resolve; }));
const switching = switched.player.play(0); switched.player.setItem({ ...item, id: 'other', sentences: [item.sentences[1]] });
resolveSwitched(payload('a')); await switching;
assert.equal(switched.audio.src, '', 'changing records cancels the old record audio');
assert.equal(switched.states.at(-1).index, 0);
switched.player.dispose();

const failed = setup(async () => { throw new Error('Missing cached WAV'); });
await failed.player.play(0);
assert.equal(failed.errors[0].message, 'Missing cached WAV');
assert.equal(failed.states.at(-1).loading, false);
assert.equal(failed.states.at(-1).playing, false);
failed.player.dispose();

const text = "It's well-known: don’t skip punctuation, spaces or 3.5!";
const tokens = practiceTokens(text);
assert.equal(tokens.map(token => token.text).join(''), text, 'word controls preserve the full original text');
assert.ok(tokens.some(token => token.word === "It's"));
assert.ok(tokens.some(token => token.word === 'don’t'));
assert.ok(tokens.some(token => token.word === 'well-known'));
assert.ok(tokens.filter(token => token.word).every(token => /^[A-Za-z]/.test(token.word)));
console.log('Practice player: 41 assertions passed; no GUI, model or filesystem audio calls.');
