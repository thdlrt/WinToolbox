import test from 'node:test';
import assert from 'node:assert/strict';
import { modelConfig } from '../desktop/src/modelConfig.ts';

const base={providers:[{id:'cloud',name:'云端',kind:'openai',base_url:'https://fixture.invalid/v1'}],preferences:{model_mode:'local',asr_model:'stale',asr_engine:'sensevoice'},roles:{transcribe:{provider_id:'cloud',model:'cloud-file'},live_asr:{provider_id:'local',model:'faster-whisper-small'},tts:{provider_id:'cloud',model:'cloud-voice'}}};
test('file/live roles override obsolete global mode independently',()=>{assert.equal(modelConfig(base).engine,'api');assert.equal(modelConfig(base).local,false);assert.equal(modelConfig(base,'live_asr').engine,'faster-whisper');assert.equal(modelConfig(base,'live_asr').model,'faster-whisper-small')});
test('speech synthesis does not inherit the ASR provider',()=>{const mixed={...base,roles:{...base.roles,transcribe:{provider_id:'local',model:'qwen-asr-0.6b'}}};assert.equal(modelConfig(mixed).engine,'qwen-asr');assert.equal(modelConfig(mixed).ttsProvider,'qwen');assert.equal(modelConfig(mixed).ttsLocal,false);mixed.roles.tts={provider_id:'local',model:'cosyvoice'};assert.equal(modelConfig(mixed).ttsProvider,'cosyvoice')});
test('cloud global mode cannot disable explicit local role',()=>{assert.equal(modelConfig({...base,preferences:{model_mode:'bailian'}},'live_asr').local,true)});
