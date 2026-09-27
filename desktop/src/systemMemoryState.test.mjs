import { strict as assert } from 'node:assert';
import { defaultMemoryPreferences, memoryDraft, memorySettingsDirty, memorySettingsParams, memorySettingsValidation } from './systemMemoryState.ts';

const baseline = memoryDraft(defaultMemoryPreferences);
assert.equal(memorySettingsDirty(baseline, defaultMemoryPreferences), false);
assert.equal(memorySettingsValidation(baseline), '');
assert.equal(memorySettingsDirty({ ...baseline, auto_enabled: true }, defaultMemoryPreferences), true);
assert.equal(memorySettingsDirty({ ...baseline, mode: 'full' }, defaultMemoryPreferences), true);
assert.equal(memorySettingsDirty({ ...baseline, interval_minutes: '60' }, defaultMemoryPreferences), true);
assert.notEqual(memorySettingsValidation({ ...baseline, auto_enabled: true, threshold_enabled: false, interval_enabled: false }), '', 'cannot enable automation with no trigger');
assert.equal(memorySettingsValidation({ ...baseline, auto_enabled: false, threshold_enabled: false, interval_enabled: false }), '', 'disabled automation need not have a trigger');
for (const patch of [{ threshold_percent: '' }, { threshold_percent: '49' }, { threshold_percent: '100' }, { interval_minutes: '0' }, { interval_minutes: '1441' }, { cooldown_minutes: '1.5' }, { cooldown_minutes: '1e2' }]) assert.notEqual(memorySettingsValidation({ ...baseline, ...patch }), '', JSON.stringify(patch));
assert.equal(memorySettingsValidation({ ...baseline, threshold_percent: '99', interval_minutes: '1440', cooldown_minutes: '1' }), '');
const fromBackend = memoryDraft({ ...defaultMemoryPreferences, automation: { running: true, last_error: 'fixture' } });
assert.equal('automation' in memorySettingsParams(fromBackend), false, 'runtime status is never written back as config');
assert.equal(memorySettingsParams({ ...baseline, interval_minutes: '30' }).interval_minutes, 30, 'RPC receives integer settings');
assert.equal(memorySettingsParams({ ...baseline, mode: 'full', auto_mode: 'default' }).auto_mode, 'default', 'manual and automatic modes stay independent');
assert.equal(memoryDraft({ mode: 'full' }).auto_enabled, false, 'legacy manual-only settings do not enable automation');
console.log('Memory settings: independent modes, valid trigger combinations, integer bounds and config-only RPC payload passed.');
