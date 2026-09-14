(async () => {
'use strict';
const $ = (s) => document.querySelector(s);
const C = window.COURSE;
const BRIDGE = 'wintoolbox.phonetics.v1';
const bridgeToken = new URLSearchParams(location.search).get('bridge');
const parentOrigin = location.origin;
let disposed=false, initialized=false, shutdownCourse=async()=>{}, requestCounter=0;
const send=(type, payload={})=>parent.postMessage({channel:BRIDGE,token:bridgeToken,type,...payload},parentOrigin);
let resolveInitial;
const initialState=new Promise(resolve=>{resolveInitial=resolve;});
window.addEventListener('message',async event=>{
  const data=event.data;
  if(event.source!==parent||event.origin!==parentOrigin||!data||data.channel!==BRIDGE||data.token!==bridgeToken)return;
  if(data.type==='init'&&!initialized&&!disposed){initialized=true;document.documentElement.dataset.theme=data.theme==='dark'?'dark':'light';resolveInitial(data.state);}
  else if(data.type==='resume'){disposed=false;document.body.inert=false;}
  else if(data.type==='theme'){document.documentElement.dataset.theme=data.theme==='dark'?'dark':'light';}
  else if(data.type==='shutdown'){disposed=true;document.body.inert=true;await shutdownCourse();send('stopped',{requestId:data.requestId});}
  else if(data.type==='result'){status(data.error||data.message||'操作完成。');}
});
send('ready');
const saved=await initialState;
if(disposed)return;
document.body.inert=false;document.querySelector('#course-loading')?.remove();
const esc = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = { done:Array.isArray(saved.done)?saved.done.filter(n=>Number.isInteger(n)&&n>=0&&n<5):[], custom:typeof saved.custom==='string'?saved.custom:'', checks:saved.checks&&typeof saved.checks==='object'?saved.checks:{}, best:Number(saved.best)||0 };
let stage=0, soundIndex=0, wordIndex=0, sentenceIndex=0, quiz=null, record=null, recordURL=null, recordTimer=null, runToken=0, cancelAudio=null, recordStream=null, permissionPromise=null, recordAttempt=0, recordBlob=null;
const lessons=[['intro','识读音标','先看懂，再跟读','02 MIN'],['sounds','关键发音','六组声音，先听再做','06 MIN'],['stress','单词重音','把力量放在正确的地方','04 MIN'],['quiz','听辨小测','耳朵先分清，嘴巴更容易跟上','03 MIN'],['talk','汇报跟读','现在，把声音放进你的汇报','05 MIN']];
const audio=$('#sample');
function save(){if(!disposed)send('change',{state:{done:[...state.done],custom:state.custom,checks:{...state.checks},best:state.best}});}
function status(t){$('#audio-status').textContent=t;}
function stop(){runToken++;if(cancelAudio)cancelAudio();audio.pause();document.querySelector('#recorded')?.pause();audio.removeAttribute('src');audio.load();if(window.speechSynthesis)window.speechSynthesis.cancel();$('.player').classList.remove('playing');}
async function play(id,label,token){
  if(disposed)return false;
  if(token===undefined){stop();token=runToken;}
  if(token!==runToken)return false;
  audio.src=window.COURSE_AUDIO?.[id]||'audio/'+encodeURIComponent(id)+'.mp3';audio.playbackRate=Number($('#speed').value);
  status(label||'正在播放示范');$('.player').classList.add('playing');
  return new Promise(resolve=>{
    const finish=()=>{clean();$('.player').classList.remove('playing');if(token===runToken)status('播放完成。现在轮到你。');resolve(token===runToken);};
    const fail=()=>{clean();$('.player').classList.remove('playing');status('示范音频未能播放，请重新进入课程后重试。');resolve(false);};
    const aborted=()=>{clean();resolve(false);};
    function clean(){audio.removeEventListener('ended',finish);audio.removeEventListener('error',fail);if(cancelAudio===aborted)cancelAudio=null;}
    cancelAudio=aborted;
    audio.addEventListener('ended',finish);audio.addEventListener('error',fail);
    audio.play().catch(fail);
  });
}
$('#speed').addEventListener('change',()=>audio.playbackRate=Number($('#speed').value));
$('#stop').addEventListener('click',()=>{stop();status('已停止。');});
function heading(n){return `<div class="section-heading"><h2>${lessons[n][1]}</h2></div>`;}
function updateNav(){
  $('#nav').innerHTML=lessons.map((l,i)=>`<button data-stage="${i}" ${i===stage?'aria-current="step"':''}><span>${String(i+1).padStart(2,'0')}${state.done.includes(i)?'<b class="check">✓</b>':''}</span>${l[1]}</button>`).join('');
  $('#progress-label').textContent=`已完成 ${state.done.length} / 5`;
  $('#progress-bar').style.width=(state.done.length*20)+'%';
  $('#lesson-count').textContent=`${String(stage+1).padStart(2,'0')} / 05 · 每次只改一个问题`;
  $('#complete').textContent=stage===4?(state.done.includes(4)?'本轮已完成 ✓':'完成本轮练习 ✓'):'完成本节，继续 →';
}
function render(n,focus=false){stop();finishRecording();stage=n;updateNav();[intro,sounds,stress,renderQuiz,talk][n]();if(focus)$('#lesson').focus({preventScroll:true});}
function setHash(name){try{history.replaceState(null,'','#'+name);}catch{}}
$('#nav').addEventListener('click',e=>{const b=e.target.closest('[data-stage]');if(b){setHash(lessons[Number(b.dataset.stage)][0]);render(Number(b.dataset.stage));}});
$('#complete').addEventListener('click',()=>{if(!state.done.includes(stage))state.done.push(stage);save();if(stage<4){render(stage+1,true);setHash(lessons[stage][0]);}else{updateNav();status('本轮练习已完成。下次可以只复习薄弱的一节。');}});

function intro(){
  $('#lesson').innerHTML=heading(0)+`<div class="panel"><p class="sub">音标记录声音。点一下下面的标记，看它告诉你什么。</p><div class="intro-ipa"><span class="slash">/</span><button data-symbol="stress" class="active" aria-label="主重音符号">ˈ</button><button data-symbol="me">me</button><button data-symbol="th">θ</button><button data-symbol="dot" aria-label="音节分界">.</button><button data-symbol="schwa">ə</button><button data-symbol="d">d</button><span class="slash">/</span></div><div id="symbol-info" class="insight" aria-live="polite"></div><div class="intro-exercise"><div><span class="label">第一个练习词</span><h3 style="margin-top:7px">method <span class="small">方法</span></h3></div><button class="primary" data-play="method">▶ 听示范，再读一次</button></div><div class="insight"><b>再记一个符号：ː</b>/iː/ 中的 ː 表示长音。重读并不只是“更大声”，通常也会更长、音高更突出。今天不用背整张表，先学会查词并找到 ˈ 后面的音节。</div></div>`;
  const info={stress:['ˈ 主重音','紧跟它的音节重读。因此 method 的 ME 更突出，后面的 thod 轻一些。'],me:['/me/ 第一音节','/m/ 双唇闭合、气流从鼻腔通过，再接 /e/。这一音节重读。'],th:['/θ/ 无声的 th','舌尖轻放上下牙之间，持续送气，喉咙不振动。th 是两个字母，这里表示一个音。'],dot:['. 音节分界','这里把 method 分成两个语音音节。分音节帮助识读，正常说话时不要在中间停顿。'],schwa:['/ə/ 轻轻放松','第二个音节的元音轻读，口腔放松。别把每个元音都读得同样饱满。'],d:['/d/ 词尾辅音','舌尖接触上齿后齿龈，形成短暂阻塞。结尾不要再加一个“的”音节。']};
  function show(key){$('#symbol-info').innerHTML=`<b>${info[key][0]}</b>${info[key][1]}`;document.querySelectorAll('[data-symbol]').forEach(b=>{b.classList.toggle('active',b.dataset.symbol===key);b.setAttribute('aria-pressed',b.dataset.symbol===key);});}
  document.querySelectorAll('[data-symbol]').forEach(b=>b.onclick=()=>show(b.dataset.symbol));show('stress');
}
function sounds(){
  const s=C.sounds[soundIndex];
  $('#lesson').innerHTML=heading(1)+`<div class="lesson-layout"><div class="sound-menu" aria-label="选择发音对比">${C.sounds.map((x,i)=>`<button data-sound="${i}" class="${i===soundIndex?'active':''}" aria-pressed="${i===soundIndex}"><b>${x.pair}</b><small>${x.tag}</small></button>`).join('')}</div><div class="panel"><h3>${s.title}</h3><p class="sub">${s.desc}</p><div class="pair-grid">${['a','b'].map(side=>{const x=s[side];return `<div class="sound-card"><span class="voicing">${x.voiced?'● 声带振动':'○ 声带不振动'}</span><span class="symbol">${x.ipa}</span><span class="sound-word">${x.word}</span><p>${x.hint}</p><button data-play="${x.word}">▶ 听例词</button></div>`;}).join('')}</div><div class="insight">${s.tip}</div><div class="transfer"><span class="label">用于汇报</span><div class="row" style="margin:10px 0"><span class="word">${s.transfer}</span><span class="ipa">${s.transferIpa}</span><button data-play="${s.transfer}">▶ 听汇报词</button></div><p class="small">${s.transferTip}</p><a class="mini-link" href="${s.video}" target="_blank" rel="noopener">看发音讲解 / 视频资源 ↗</a></div></div></div>`;
  document.querySelectorAll('[data-sound]').forEach(b=>b.onclick=()=>{stop();soundIndex=Number(b.dataset.sound);sounds();});
}
function stress(){
  const w=C.words[wordIndex];
  $('#lesson').innerHTML=heading(2)+`<div class="word-tabs" aria-label="学术词汇">${C.words.map((x,i)=>`<button data-word="${i}" class="${i===wordIndex?'active':''}" aria-pressed="${i===wordIndex}">${x.word}</button>`).join('')}</div><div class="panel"><div class="stress-board"><span class="label">${w.meaning} · 先听，再选重读部分</span><div class="stress-parts">${w.parts.map((p,i)=>`<button data-part="${i}">${p}</button>`).join('')}</div><button class="primary" data-play="${w.word}">▶ 听这个词</button><p id="stress-feedback" class="feedback" aria-live="polite">你觉得哪一部分最突出？点击它。</p><p id="stress-ipa" class="small" hidden>${w.ipa}</p></div><div class="insight"><b>先听重音，再看音标</b>ˈ 后面的音节重读；ˌ 是次重音。按钮是拼写记忆分块，实际声音以音标和录音为准。</div></div>`;
  document.querySelectorAll('[data-word]').forEach(b=>b.onclick=()=>{stop();wordIndex=Number(b.dataset.word);stress();});
  document.querySelectorAll('[data-part]').forEach(b=>b.onclick=()=>{const correct=Number(b.dataset.part)===w.stress;b.classList.add(correct?'correct':'wrong');$('#stress-feedback').textContent=correct?'✓ 对了。'+w.tip:'再听一次：留意更突出、通常也更长的音节。';$('#stress-feedback').classList.toggle('error',!correct);if(correct)$('#stress-ipa').hidden=false;});
}
function newQuiz(){quiz={index:0,correct:0,answered:false,heard:false,items:C.quiz.map(p=>({...p,target:Math.random()<.5?p.a:p.b})).sort(()=>Math.random()-.5)};}
function renderQuiz(){
  if(!quiz)newQuiz();
  if(quiz.index>=quiz.items.length){$('#lesson').innerHTML=heading(3)+`<div class="panel quiz"><p class="label">本轮听辨完成</p><p class="score">${quiz.correct} <span class="small">/ ${quiz.items.length}</span></p><p class="sub">${quiz.correct>=5?'听辨已经比较稳定，接下来把声音带入汇报。':'回到「关键发音」，重听刚才拿不准的声音，再试一轮。'}</p><p class="small">这是词语听辨结果，不是你的发音评分。</p><button id="quiz-restart" class="primary">再练一轮</button></div>`;$('#quiz-restart').onclick=()=>{newQuiz();renderQuiz();};return;}
  const q=quiz.items[quiz.index];quiz.answered=false;quiz.heard=false;
  $('#lesson').innerHTML=heading(3)+`<div class="panel"><div class="quiz"><span class="label">第 ${quiz.index+1} / ${quiz.items.length} 题</span><p class="sub" style="margin-top:15px">听到的是哪一个词？可以重播。</p><button id="quiz-play" class="listen-orb" aria-label="播放待辨认的单词">▶</button><div class="quiz-choices"><button data-answer="${q.a}" disabled>${q.a}</button><button data-answer="${q.b}" disabled>${q.b}</button></div><p class="feedback" id="quiz-feedback" aria-live="polite">先点播放，听完后选择。</p><button id="quiz-next" hidden>下一题 →</button></div></div>`;
  $('#quiz-play').onclick=async()=>{const current=quiz.index;const ok=await play(q.target,'听辨题：请听单词');if(ok&&stage===3&&quiz.index===current&&!quiz.answered){quiz.heard=true;document.querySelectorAll('[data-answer]').forEach(b=>b.disabled=false);$('#quiz-feedback').textContent='选择你刚才听到的词。';}};
  document.querySelectorAll('[data-answer]').forEach(b=>b.onclick=()=>{if(quiz.answered||!quiz.heard)return;quiz.answered=true;const ok=b.dataset.answer===q.target;if(ok)quiz.correct++;document.querySelectorAll('[data-answer]').forEach(x=>{x.disabled=true;if(x.dataset.answer===q.target)x.classList.add('correct');});if(!ok)b.classList.add('wrong');$('#quiz-feedback').textContent=(ok?'✓ 听对了。':'这次是 '+q.target+'。')+' '+q.hint;$('#quiz-next').hidden=false;});
  $('#quiz-next').onclick=()=>{stop();quiz.index++;if(quiz.index===quiz.items.length){state.best=Math.max(state.best,quiz.correct);save();}renderQuiz();};
}
function talk(){
  const s=C.sentences[sentenceIndex];
  $('#lesson').innerHTML=heading(4)+`<div class="sentence-list">${C.sentences.map((x,i)=>`<button data-sentence="${i}" class="${i===sentenceIndex?'active':''}" aria-pressed="${i===sentenceIndex}">${x.title.split(' · ')[0]}</button>`).join('')}</div><div class="panel"><span class="label">${s.title}</span><p class="sentence">${s.marked}</p><p class="small">${s.zh}</p><div class="row"><button class="primary" data-play="${s.id}">▶ 听整句</button><button id="shadow">▷ 分段跟读 · 留时间给你</button></div><div class="row chunk-row">${s.chunks.map((c,i)=>`<button data-play="${s.id}-${i}">▶ ${esc(c)}</button>`).join('')}</div><div class="insight">${s.focus}<br><span class="small">斜线是短停顿；高亮是建议强调处。示范音频提供自然读法，强调可随语境改变。</span></div><div class="rehearsal-row"><button id="record">● 录下我的跟读</button><span id="record-status" class="small" role="status">录音仅在本机回放，不上传。</span><a id="download-record" class="mini-link" hidden>保存本次录音</a></div><audio id="recorded" class="recorded" controls hidden></audio><div class="self-checks">${['重音位置清楚','词尾没有多余元音','停顿自然'].map((v,i)=>`<label><input type="checkbox" data-check="${s.id}-${i}" ${state.checks[s.id+'-'+i]?'checked':''}>${v}</label>`).join('')}</div><details class="custom"><summary>换成我的汇报稿</summary><p class="small">用 / 标停顿，用 **关键词** 标重点。输入后自动保存；系统朗读只读文字，不会自动按标记强调。</p><textarea id="custom-text" maxlength="20000" aria-label="我的汇报稿" placeholder="Today, / I will present our **method**.">${esc(state.custom)}</textarea><div class="row"><button id="custom-speak">▶ 系统朗读我的稿子</button><button id="custom-export">导出稿子 .md</button></div><p class="small">预设示范可离线播放；这里的英文系统语音取决于设备。你也可以只用标记稿自行跟读、录音。</p></details></div>`;
  document.querySelectorAll('[data-sentence]').forEach(b=>b.onclick=()=>{stop();finishRecording();sentenceIndex=Number(b.dataset.sentence);talk();});
  $('#shadow').onclick=async()=>{stop();const token=runToken;for(let i=0;i<s.chunks.length;i++){if(!await play(s.id+'-'+i,'示范 '+(i+1)+' / '+s.chunks.length,token))return;const seconds=Math.max(3,Math.ceil(audio.duration)+1);for(let t=seconds;t>0;t--){if(token!==runToken)return;status('轮到你跟读 · '+t+' 秒');await new Promise(r=>setTimeout(r,1000));}}if(token===runToken)status('分段跟读完成。试着连起来说整句。');};
  $('#record').onclick=recordClick;
  document.querySelectorAll('[data-check]').forEach(c=>c.onchange=()=>{state.checks[c.dataset.check]=c.checked;save();});
  $('#custom-text').oninput=e=>{state.custom=e.target.value.slice(0,20000);save();};
  $('#custom-export').onclick=()=>download('我的汇报跟读稿.md','# 我的汇报跟读稿\n\n'+state.custom+'\n');
  $('#custom-speak').onclick=()=>{const text=state.custom.replace(/\*\*/g,'').replace(/\//g,', ').trim();if(!text){status('先在文本框填入汇报稿。');return;}stop();if(!window.speechSynthesis){status('当前环境没有系统语音，请使用预设音频或自行跟读。');return;}const voice=speechSynthesis.getVoices().find(v=>v.lang.startsWith('en-US'))||speechSynthesis.getVoices().find(v=>v.lang.startsWith('en'));if(!voice){status('没有可用的英语系统语音。预设示范仍可播放。');return;}const u=new SpeechSynthesisUtterance(text);u.voice=voice;u.lang=voice.lang;u.rate=Number($('#speed').value);u.onend=()=>status('系统朗读完成。');u.onerror=()=>status('系统朗读不可用，请使用预设示范。');speechSynthesis.speak(u);status('系统合成朗读 · 我的汇报稿');};
}
function finishRecording(){recordAttempt++;if(record&&record.state!=='inactive')record.stop();clearTimeout(recordTimer);}
async function recordClick(){
  if(disposed)return;
  if(record&&record.state==='recording'){finishRecording();return;}
  if(permissionPromise)return;
  stop();
  const button=$('#record'), label=$('#record-status'), playback=$('#recorded'), link=$('#download-record');
  if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){label.textContent='当前设备不支持课程录音；仍可播放示范并自行跟读。';return;}
  button.disabled=true;label.textContent='请允许使用麦克风…';const attempt=++recordAttempt;
  let stream;
  const request=navigator.mediaDevices.getUserMedia({audio:true});permissionPromise=request;
  try {
    stream=await request;
    if(disposed||attempt!==recordAttempt||!button.isConnected){stream.getTracks().forEach(t=>t.stop());return;}
    recordStream=stream;
    const chunks=[];record=new MediaRecorder(stream);const localRecord=record;
    record.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
    record.onstop=()=>{
      clearTimeout(recordTimer);stream.getTracks().forEach(t=>t.stop());if(recordStream===stream)recordStream=null;if(record===localRecord)record=null;
      if(disposed||!button.isConnected)return;
      button.disabled=false;button.textContent='● 再录一次';button.classList.remove('recording');
      if(recordURL)URL.revokeObjectURL(recordURL);
      const blob=new Blob(chunks,{type:localRecord.mimeType});recordBlob=blob;recordURL=URL.createObjectURL(blob);playback.src=recordURL;playback.hidden=false;
      const filename='我的跟读-'+new Date().toISOString().replace(/[:.]/g,'-')+(blob.type.includes('mp4')?'.m4a':'.webm');
      link.removeAttribute('href');link.setAttribute('role','button');link.tabIndex=0;
      const exportRecording=async()=>{try{const buffer=await blob.arrayBuffer();if(!disposed)send('export',{requestId:++requestCounter,name:filename,bytes:Array.from(new Uint8Array(buffer))});}catch{status('录音导出失败，请重试。');}};
      link.onclick=exportRecording;link.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();void exportRecording();}};link.hidden=false;
      label.textContent='录音完成，可回听或保存；离开课程后临时录音会清除。';
    };
    record.start();button.disabled=false;button.textContent='■ 停止录音';button.classList.add('recording');label.textContent='正在录音 · 最长 90 秒';recordTimer=setTimeout(finishRecording,90000);
  } catch(e){if(stream)stream.getTracks().forEach(t=>t.stop());if(!disposed&&button.isConnected){button.disabled=false;label.textContent='未能打开麦克风，请检查权限后重试。';}}
  finally{if(permissionPromise===request)permissionPromise=null;}
}
function download(name,text){if(!disposed)send('export',{requestId:++requestCounter,name,bytes:Array.from(new TextEncoder().encode(text))});}
shutdownCourse=async()=>{
  disposed=true;recordAttempt++;stop();finishRecording();
  document.querySelectorAll('audio').forEach(element=>{element.pause();element.removeAttribute('src');element.load();});
  if(recordStream){recordStream.getTracks().forEach(track=>track.stop());recordStream=null;}
  const pending=permissionPromise;if(pending){try{const stream=await pending;stream.getTracks().forEach(track=>track.stop());}catch{}}
  if(recordURL){URL.revokeObjectURL(recordURL);recordURL=null;}recordBlob=null;
};
document.addEventListener('click',event=>{
  const link=event.target.closest('a[href]');if(!link)return;
  const url=new URL(link.href,location.href);
  if(url.protocol==='http:'||url.protocol==='https:'){event.preventDefault();if(!disposed)send('external',{requestId:++requestCounter,url:url.href});}
});
$('#export-progress').onclick=()=>{download('音标微课-练习记录.md','# 学术汇报音标练习\n\n日期：'+new Date().toLocaleDateString()+'\n\n'+lessons.map((l,i)=>'- ['+(state.done.includes(i)?'x':' ')+'] '+l[1]).join('\n')+'\n\n听辨最佳：'+state.best+'/6（非发音评分）\n\n## 我的汇报稿\n\n'+state.custom+'\n');};
$('#lesson').addEventListener('click',e=>{const b=e.target.closest('[data-play]');if(b)play(b.dataset.play,'示范 · '+b.dataset.play.replace(/-\d+$/,''));});
window.addEventListener('pagehide',()=>{void shutdownCourse();});
if(window.speechSynthesis)speechSynthesis.getVoices();
const initial=lessons.findIndex(l=>'#'+l[0]===location.hash);render(initial<0?0:initial);
})();
