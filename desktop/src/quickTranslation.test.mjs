import { strict as assert } from 'node:assert';
import { nextQuickTranslation } from './quickTranslation.ts';

const snapshot = {text:'Selected text',foreign:true,settings:{auto_translate:true},clipboard_text:'Clipboard text',clipboard_foreign:true};
const attempted = new Set();
let next = nextQuickTranslation(snapshot,attempted);
assert.equal(next.source,'selection');
attempted.add(next.key);
next=nextQuickTranslation(snapshot,attempted);
assert.equal(next.source,'clipboard','clipboard waits for selected-text attempt');
attempted.add(next.key);
assert.equal(nextQuickTranslation(snapshot,attempted),undefined,'failed automatic requests must not loop');
assert.equal(nextQuickTranslation({...snapshot,clipboard_text:'New OCR result'},attempted).source,'clipboard','a new OCR result triggers translation');
assert.equal(nextQuickTranslation({...snapshot,foreign:false,clipboard_foreign:false},new Set()),undefined,'Chinese and excluded text stay quiet');
assert.equal(nextQuickTranslation({...snapshot,settings:{auto_translate:false}},new Set()),undefined);
assert.equal(nextQuickTranslation({...snapshot,initial_job:{id:'file-lock-job'}},new Set()),undefined,'wait for the initial context job');
assert.equal(nextQuickTranslation({...snapshot,translation:{},clipboard_translation:{}},new Set()),undefined,'ready results are not submitted again');
console.log('PASS automatic clipboard translation, OCR chaining, sequential sources and failed-request suppression');
