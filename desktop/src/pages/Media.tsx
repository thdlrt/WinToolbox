import { useEffect, useState } from 'react';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { FileAudio, SlidersHorizontal, Trash2, Upload, X } from 'lucide-react';
import { isDesktop, native, rpc, type Job } from '../api';
import { useApp } from '../context';
import { basename, Button, CheckField, cx, Field, IconButton, Notice, PathInput, Section } from '../ui';
import ToolJobs from '../ToolJobs';
import { modelConfig } from '../modelConfig';

const engineModels: Record<string, { value: string; label: string }[]> = {
  api: [{ value: '', label: '使用设置中的默认转写模型' }],
  'faster-whisper': [{ value: 'faster-whisper-small', label: 'Whisper Small · 轻量' }, { value: 'faster-whisper-turbo', label: 'Whisper large-v3-turbo · 快速' }, { value: 'faster-whisper-large-v3', label: 'Whisper large-v3 · 精细' }],
  'qwen-asr': [{ value: 'qwen-asr-0.6b', label: 'Qwen3-ASR 0.6B · 轻量' }, { value: 'qwen-asr-1.7b', label: 'Qwen3-ASR 1.7B · 标准' }],
  sensevoice: [{ value: 'sensevoice-small', label: 'SenseVoice Small' }],
};
export default function MediaPage() {
  const { run, error, track, navigate, connected, settings } = useApp();
  const config = modelConfig(settings);
  const [paths, setPaths] = useState<string[]>([]);
  const [manualPaths, setManualPaths] = useState('');
  const [showPaths, setShowPaths] = useState(false);
  const [output, setOutput] = useState('');
  const [engine, setEngine] = useState(config.engine);
  const [model, setModel] = useState(config.model);
  const [language, setLanguage] = useState('auto');
  const [targetLanguage, setTargetLanguage] = useState('zh');
  const [options, setOptions] = useState({ transcribe: true, translate: true, summary: false, diarize: false, burn: false, dub: false, enhance: false });
  const [summaryMode, setSummaryMode] = useState('general');
  const [summaryPrompt, setSummaryPrompt] = useState('');
  const [tts, setTts] = useState(config.local ? 'cosyvoice' : config.mode === 'bailian' ? 'qwen' : 'edge');
  const [voice, setVoice] = useState(config.mode ? '' : 'zh-CN-XiaoxiaoNeural');
  const [reference, setReference] = useState('');
  const [referenceText, setReferenceText] = useState('');
  const [subtitlePath, setSubtitlePath] = useState('');
  const [separateBackground, setSeparateBackground] = useState(false);
  const [enhancementModel, setEnhancementModel] = useState('realesrgan-x4plus');
  const [rate, setRate] = useState(1);
  const [volume, setVolume] = useState(1);
  const [scale, setScale] = useState(2);
  const [tile, setTile] = useState(256);
  const [advanced, setAdvanced] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (config.mode) { setEngine(config.engine); setModel(config.model); setTts(config.local ? 'cosyvoice' : 'qwen'); setVoice(''); }
  }, [config.mode, config.engine, config.model, config.local]);
  const add = (values: string[]) => setPaths(old => [...new Set([...old, ...values.filter(Boolean)])]);
  useEffect(() => {
    if (!isDesktop()) return;
    let off: (() => void) | undefined; let disposed = false;
    void getCurrentWebviewWindow().onDragDropEvent(event => { setDragging(event.payload.type === 'over' || event.payload.type === 'enter'); if (event.payload.type === 'drop') add(event.payload.paths); }).then(unlisten => { if (disposed) unlisten(); else off = unlisten; }).catch(error);
    return () => { disposed = true; off?.(); };
  }, [error]);
  const choose = async () => { const result = await run(() => native.files(true, [{ name: '音频与视频', extensions: ['mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg', 'opus', 'mp4', 'mkv', 'mov', 'webm', 'avi', 'wma', 'm4v'] }])); if (result) add(result); };
  const submit = async () => {
    setBusy(true);
    const job = await run(() => rpc<Job>('jobs.submit', { tool: 'media', params: { paths, ...(output ? { output_dir: output } : {}), engine: config.mode ? config.engine : engine, model: config.mode ? config.model : model, ...(config.local ? { device: config.device, compute_type: config.computeType } : {}), language, target_language: targetLanguage, ...options, summary_mode: summaryMode, summary_prompt: summaryPrompt, tts_provider: config.mode ? (config.local ? 'cosyvoice' : 'qwen') : tts, voice, reference_audio: reference, reference_text: referenceText, subtitle_path: subtitlePath, separate_background: separateBackground, speech_rate: rate, volume, enhancement_scale: scale, enhancement_tile: tile, enhancement_model: enhancementModel } }));
    if (job) { track(job); } setBusy(false);
  };

  return <>
    
    <div className="media-workspace">
      <Section title="添加文件" action={<span className="muted">{paths.length ? `${paths.length} 个文件` : '音频 / 视频'}</span>}>
        <button className={cx('upload-zone', dragging && 'dragging', paths.length > 0 && 'compact')} onClick={choose}><Upload size={21} strokeWidth={1.6} /><strong>{paths.length ? '继续添加文件' : '拖入文件，或点击选择'}</strong><span>MP3、WAV、M4A、MP4、MKV 等常见格式</span></button>
        {paths.length > 0 && <div className="file-list">{paths.map(path => <div className="file-row" key={path}><FileAudio size={19} /><div><strong>{basename(path)}</strong><small title={path}>{path}</small></div><IconButton label={`移除 ${basename(path)}`} onClick={() => setPaths(old => old.filter(item => item !== path))}><X size={16} /></IconButton></div>)}<div className="file-list-footer"><Button variant="ghost" onClick={() => setPaths([])}><Trash2 size={15} />清空</Button></div></div>}
        <button className="text-link spaced" onClick={() => setShowPaths(!showPaths)}>通过文件路径添加</button>
        {showPaths && <div className="manual-paths"><textarea placeholder={'每行一个完整路径，例如：\nD:\\Media\\会议录音.mp3'} rows={3} value={manualPaths} onChange={e => setManualPaths(e.target.value)} /><Button onClick={() => { add(manualPaths.split(/\r?\n/).map(value => value.trim().replace(/^"|"$/g, ''))); setManualPaths(''); setShowPaths(false); }}>添加路径</Button></div>}
      </Section>
      <Section title="处理步骤">
        <div className="step-options"><CheckField checked={options.transcribe} onChange={v => setOptions({ ...options, transcribe: v })} label="语音转写" /><CheckField checked={options.translate} onChange={v => setOptions({ ...options, translate: v })} label="翻译字幕" /><CheckField checked={options.summary} onChange={v => setOptions({ ...options, summary: v })} label="内容总结" /><CheckField checked={options.diarize} onChange={v => setOptions({ ...options, diarize: v })} label="区分说话人" /></div>
        <div className="section-divider" /><div className="step-options"><CheckField checked={options.burn} onChange={v => setOptions({ ...options, burn: v })} label="压制硬字幕" hint="将双语字幕写入视频画面" /><CheckField checked={options.dub} onChange={v => setOptions({ ...options, dub: v })} label="生成配音" /><CheckField checked={options.enhance} onChange={v => setOptions({ ...options, enhance: v })} label="视频画质增强" /></div>
        {options.summary && <div className="nested-options"><Field label="总结模板"><select value={summaryMode} onChange={e => setSummaryMode(e.target.value)}><option value="general">通用 · 重点与概览</option><option value="course">课程 · 知识点与学习线索</option><option value="meeting">会议 · 决议与行动项</option><option value="interview">面试 · 问答与评价要点</option></select></Field><Field label="补充要求（可选）"><textarea rows={2} value={summaryPrompt} onChange={e => setSummaryPrompt(e.target.value)} placeholder="例如：保留技术术语，重点整理演示步骤。" /></Field></div>}
        {options.dub && <div className="nested-options"><div className="form-grid">{!config.mode && <Field label="配音引擎"><select value={tts} onChange={e => { setTts(e.target.value); setVoice(e.target.value === 'edge' ? 'zh-CN-XiaoxiaoNeural' : ''); }}><option value="edge">Edge TTS · 在线声音</option><option value="qwen">百炼 Qwen TTS · API</option><option value="cosyvoice">CosyVoice · 本地</option></select></Field>}<Field label="声音 ID"><input value={voice} onChange={e => setVoice(e.target.value)} placeholder={tts === 'edge' ? 'zh-CN-XiaoxiaoNeural' : '使用引擎默认声音'} /></Field></div><Field label={config.local ? "参考音频" : "参考音频（可选）"} hint={config.local ? "本地配音需要安装 CosyVoice，并提供参考音频。" : "用于支持参考声音的模型。"}><PathInput value={reference} onChange={setReference} file placeholder="选择参考音频" onError={error} /></Field>{(tts === 'cosyvoice' || reference) && <Field label="参考音频文字" hint="填写参考音频中说出的内容，供参考音色模型使用。"><textarea rows={2} value={referenceText} onChange={e => setReferenceText(e.target.value)} placeholder="参考音频的准确文字" /></Field>}<CheckField checked={separateBackground} onChange={setSeparateBackground} label="分离并保留原背景声音" hint="使用 Demucs 去除原人声，再混合新配音。" /><div className="form-grid top-gap"><Field label={`语速 · ${rate.toFixed(1)}×`}><input type="range" min="0.5" max="2" step="0.1" value={rate} onChange={e => setRate(Number(e.target.value))} /></Field><Field label={`配音音量 · ${Math.round(volume * 100)}%`}><input type="range" min="0.1" max="2" step="0.1" value={volume} onChange={e => setVolume(Number(e.target.value))} /></Field></div></div>}
        {options.enhance && <div className="nested-options"><Field label="增强模型"><input list="enhancement-models" value={enhancementModel} onChange={e => setEnhancementModel(e.target.value)} /><datalist id="enhancement-models"><option value="realesrgan-x4plus">通用画面</option><option value="realesr-animevideov3">动漫视频</option><option value="realesrgan-x4plus-anime">动漫图像</option></datalist></Field><div className="form-grid"><Field label="放大倍率"><select value={scale} onChange={e => setScale(Number(e.target.value))}><option value={2}>2×（最高 4K）</option><option value={4}>4×（最高 4K）</option></select></Field><Field label="分块尺寸" hint="显存较小时选择较小尺寸。"><select value={tile} onChange={e => setTile(Number(e.target.value))}><option value={128}>128 px</option><option value={256}>256 px</option><option value={512}>512 px</option></select></Field></div></div>}
      </Section>
      <Section title="输出" action={<Button variant="ghost" onClick={() => setAdvanced(!advanced)}><SlidersHorizontal size={15} />{advanced ? '收起高级设置' : '高级设置'}</Button>}>
        <div className="current-model"><span>{config.label}</span><button className="text-link" onClick={() => navigate('settings')}>更改</button></div>{advanced && !config.mode && <div className="form-grid"><Field label="识别引擎"><select value={engine} onChange={e => { setEngine(e.target.value); setModel(engineModels[e.target.value][0].value); }}><option value="api">API · 云端识别</option><option value="faster-whisper">faster-whisper · 本地</option><option value="qwen-asr">Qwen3-ASR · 本地</option><option value="sensevoice">SenseVoice · 本地</option></select></Field><Field label="识别模型"><select value={model} onChange={e => setModel(e.target.value)}>{(engineModels[engine] || []).map(item => <option value={item.value} key={item.value}>{item.label}</option>)}</select></Field></div>}
        {advanced && !config.mode && engine !== 'api' && <Notice>首次使用前，请在“设置 → 模型 → 高级设置”安装对应模型和运行环境。<button className="inline-link" onClick={() => navigate('settings')}>管理模型</button></Notice>}
        {advanced && <div className="form-grid top-gap"><Field label="原始语言"><select value={language} onChange={e => setLanguage(e.target.value)}><option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option><option value="fr">法语</option><option value="de">德语</option></select></Field><Field label="翻译目标语言"><select value={targetLanguage} onChange={e => setTargetLanguage(e.target.value)}><option value="zh">简体中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></select></Field></div>}
        {advanced && <Field label="已有字幕（可选）" hint="导入 SRT、VTT 或字幕 JSON 后，将优先使用该字幕；可关闭语音转写。"><PathInput value={subtitlePath} onChange={setSubtitlePath} file placeholder="选择已有字幕文件" onError={error} /></Field>}
        <Field label="输出目录" hint="留空时保存在工具箱的数据目录中。"><PathInput value={output} onChange={setOutput} placeholder="默认任务目录" onError={error} /></Field>
      </Section>
    <div className="submit-bar"><span className="muted">{paths.length ? `${paths.length} 个文件` : '未选择文件'}</span><div className="button-row"><Button variant="primary" busy={busy} disabled={!connected || !paths.length || !Object.values(options).some(Boolean)} onClick={submit}>开始处理</Button></div></div></div>
    <ToolJobs scope="media" title="处理结果" />
  </>;
}
