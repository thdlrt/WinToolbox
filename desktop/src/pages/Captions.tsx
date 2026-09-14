import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { GripHorizontal, Maximize2, MousePointer2, Play, RefreshCw, Square, X } from 'lucide-react';
import { rpc } from '../api';
import { useApp } from '../context';
import { useCaptions, type CaptionMode, type CaptionState } from '../captions';
import { CaptionPager, type CaptionFrame, type CaptionLayout } from '../captionText';
import { modelConfig } from '../modelConfig';
import { Button, CheckField, cx, Field, IconButton, Notice, Section } from '../ui';

interface Device { id: string; name: string; kind: string }
const modes: { id: CaptionMode; label: string }[] = [{ id: 'bilingual', label: '双语' }, { id: 'translation', label: '仅译文' }, { id: 'original', label: '仅原文' }];
const languages = [{ id: 'en', label: '英语' }, { id: 'zh', label: '中文' }, { id: 'ja', label: '日语' }, { id: 'ko', label: '韩语' }, { id: 'fr', label: '法语' }, { id: 'de', label: '德语' }, { id: 'es', label: '西班牙语' }];

function CaptionText({ state, failure = '', preview = false }: { state: CaptionState; failure?: string; preview?: boolean }) {
  const root = useRef<HTMLDivElement>(null);
  const originalMetric = useRef<HTMLParagraphElement>(null);
  const translationMetric = useRef<HTMLParagraphElement>(null);
  const pager = useRef(new CaptionPager());
  const [layout, setLayout] = useState<CaptionLayout>();
  const [frame, setFrame] = useState<CaptionFrame>({ pageIndex: 0, pageCount: 0, skipped: false, waiting: false });
  const mode = state.options.display_mode;
  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const measure = () => {
      const original = originalMetric.current, translation = translationMetric.current;
      if (!original || !translation) return;
      const style = getComputedStyle(element);
      const width = Math.max(1, element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - 3);
      const createMeasurer = (metric: HTMLElement) => {
        const font = getComputedStyle(metric), canvas = document.createElement('canvas').getContext('2d')!;
        canvas.font = `${font.fontWeight} ${font.fontSize} ${font.fontFamily}`;
        const spacing = parseFloat(font.letterSpacing) || 0;
        return { key: canvas.font, measure: (value: string) => canvas.measureText(value).width + Array.from(value).length * spacing };
      };
      const a = createMeasurer(original), b = createMeasurer(translation);
      const key = `${width}/${a.key}/${b.key}/${mode}`;
      setLayout(old => old?.key === key ? old : { key, width, originalLines: mode === 'bilingual' ? 1 : 2, translationLines: 2, originalMeasure: a.measure, translationMeasure: b.measure });
    };
    measure(); const observer = new ResizeObserver(measure); observer.observe(element);
    return () => observer.disconnect();
  }, [mode, state.options.font_size]);
  useEffect(() => {
    if (!layout) return;
    const next = pager.current.update(state.session_id, state.active, mode, state.options.target_language, state.segments, layout, performance.now());
    setFrame(next);
  }, [state, layout, mode]);
  useEffect(() => {
    const timer = setInterval(() => {
      const next = pager.current.tick(performance.now());
      setFrame(old => old.page === next.page && old.pageIndex === next.pageIndex && old.pageCount === next.pageCount && old.skipped === next.skipped && old.waiting === next.waiting ? old : next);
    }, 150);
    return () => clearInterval(timer);
  }, []);
  const failed = failure || (['error', 'failed'].includes(state.status) ? state.message : '');
  const status = failed || (!state.active ? '已停止' : !frame.page ? state.status === 'starting' ? '正在启动…' : state.status === 'reconnecting' ? '正在重新连接…' : frame.waiting ? '等待本句翻译…' : '等待完整语句…' : '');
  return <div ref={root} className={cx('caption-text', `mode-${mode}`, preview && 'caption-preview-text')} style={{ '--caption-font-size': `${state.options.font_size}px`, '--caption-opacity': state.options.background_opacity } as CSSProperties}>
    <div className="caption-metrics" aria-hidden="true"><p ref={originalMetric} className="caption-original">M</p><p ref={translationMetric} className="caption-translation">M</p></div>
    {status ? <div className={cx('caption-status', failed && 'caption-error')} role={failed ? 'alert' : 'status'}>{status}</div> : <div className="caption-lines" aria-live="off" data-cue-id={frame.page?.cueId} data-page-index={frame.pageIndex}>
      {frame.page?.original && <p className="caption-original">{frame.page.original}</p>}
      {frame.page?.translation && <p className="caption-translation">{frame.page.translation}</p>}
    </div>}
    {!status && <div className="caption-page-meta">{frame.page?.warning && <span>{frame.page.warning}</span>}{frame.skipped && <span>已跳过较早字幕</span>}{frame.pageCount > 1 && <span className="caption-page-number">{frame.pageIndex + 1} / {frame.pageCount}</span>}</div>}
  </div>;
}
export default function CaptionsPage() {
  const { connected, settings, navigate } = useApp();
  const captions = useCaptions(connected);
  const { state, failure, busy, windowState, configure } = captions;
  const [devices, setDevices] = useState<Device[]>([]);
  const [fontSize, setFontSize] = useState(state.options.font_size);
  const [opacity, setOpacity] = useState(state.options.background_opacity);
  const [deviceBusy, setDeviceBusy] = useState(false);
  useEffect(() => { setFontSize(state.options.font_size); setOpacity(state.options.background_opacity); }, [state.options.font_size, state.options.background_opacity]);
  const loadDevices = async () => {
    setDeviceBusy(true);
    try { const result = await rpc<{ devices: Device[] }>('live.devices'); setDevices(result.devices.filter(item => item.kind === 'system')); }
    catch (reason) { captions.reportFailure(reason); }
    finally { setDeviceBusy(false); }
  };
  useEffect(() => { if (connected) void loadDevices(); }, [connected]);
  const config = modelConfig(settings);
  const previewState = { ...state, options: { ...state.options, font_size: fontSize, background_opacity: opacity } };
  return <div className="captions-workspace">
    <Section>
      <div className="caption-page-top"><button className="current-mode-link" disabled={state.active} onClick={() => navigate('settings')}>{config.label}</button><Button variant={state.active ? 'secondary' : 'primary'} busy={busy} disabled={!connected} onClick={() => void (state.active ? captions.stop() : captions.start())}>{state.active ? <Square size={14} /> : <Play size={14} />}{state.active ? '停止字幕' : '开始字幕'}</Button></div>
      <Field label="电脑声音"><div className="caption-device-row"><select disabled={state.active || !connected} value={state.options.system_id} onChange={event => void configure({ system_id: event.target.value })}><option value="">默认播放设备</option>{devices.map(device => <option key={device.id} value={device.id}>{device.name}</option>)}</select><IconButton label="刷新声音设备" disabled={state.active || deviceBusy || !connected} onClick={() => void loadDevices()}><RefreshCw size={16} className={cx(deviceBusy && 'spin')} /></IconButton></div></Field>
      <div className="caption-language-options"><Field label="原语言" hint={state.active ? '停止字幕后可更改。' : undefined}><select aria-label="原语言" disabled={state.active || !connected || busy} value={state.options.source_language} onChange={event => void configure({ source_language: event.target.value })}><option value="auto">自动识别</option>{languages.map(language => <option key={language.id} value={language.id}>{language.label}</option>)}</select></Field><Field label="翻译为"><select aria-label="翻译为" disabled={!connected || busy || state.options.display_mode === 'original'} value={state.options.target_language} onChange={event => void configure({ target_language: event.target.value })}>{languages.map(language => <option key={language.id} value={language.id}>{language.label}</option>)}</select></Field></div>
      <div className="caption-options"><Field label="显示"><div className="segmented caption-modes">{modes.map(mode => <button key={mode.id} disabled={!connected} className={cx(state.options.display_mode === mode.id && 'selected')} aria-label={mode.label} aria-pressed={state.options.display_mode === mode.id} onClick={() => void configure({ display_mode: mode.id })}>{mode.label}</button>)}</div></Field><Field label={`字号 · ${fontSize}`}><input type="range" min={18} max={48} step={2} disabled={!connected} value={fontSize} onChange={event => setFontSize(Number(event.target.value))} onPointerUp={() => void configure({ font_size: fontSize })} onKeyUp={() => void configure({ font_size: fontSize })} onBlur={() => { if (fontSize !== state.options.font_size) void configure({ font_size: fontSize }); }} /></Field><Field label={`背景不透明度 · ${Math.round(opacity * 100)}%`}><input type="range" min={0} max={1} step={0.01} disabled={!connected} value={opacity} onChange={event => setOpacity(Number(event.target.value))} onPointerUp={() => void configure({ background_opacity: opacity })} onKeyUp={() => void configure({ background_opacity: opacity })} onBlur={() => { if (opacity !== state.options.background_opacity) void configure({ background_opacity: opacity }); }} /></Field></div>
      <div className="caption-preview" style={{ height: Math.max(160, fontSize * 4.2 + 35) }}><CaptionText state={previewState} preview /></div>
      <div className="caption-page-footer"><CheckField checked={windowState.click_through} disabled={!connected || !windowState.visible || busy} onChange={value => void captions.clickThrough(value)} label="鼠标穿透" /><Button disabled={!connected || busy} onClick={() => void captions.show()}><Maximize2 size={15} />显示字幕窗</Button></div>
      <p className="small-note caption-shortcuts">Ctrl + Alt + L 解除穿透 · Ctrl + Alt + C 显示 / 隐藏字幕窗（继续识别）</p>
    </Section>
    {failure && <Notice tone="warning">{failure}</Notice>}
    <p className="small-note">只采集电脑播放声音；关闭字幕窗或点击停止后结束采集。</p>
    <details className="details caption-fullscreen-help"><summary>全屏播放提示</summary><p className="small-note">播放器独占全屏时，请改用窗口或无边框全屏。</p></details>
  </div>;
}

export function CaptionOverlay() {
  const { connected } = useApp();
  const captions = useCaptions(connected);
  const { state, failure, busy, windowState } = captions;
  return <div className={cx('caption-window', windowState.click_through && 'click-through')} style={{ '--caption-opacity': state.options.background_opacity } as CSSProperties}>
    <div className="caption-toolbar"><span className="caption-drag" data-tauri-drag-region><GripHorizontal size={16} /><span data-tauri-drag-region>{state.active ? '字幕识别中' : '实时字幕'}</span></span><div className="flex-spacer" /><IconButton label={state.active ? '停止字幕' : '开始字幕'} disabled={busy || !connected} onClick={() => void (state.active ? captions.stop() : captions.start())}>{state.active ? <Square size={14} /> : <Play size={14} />}</IconButton><IconButton label="鼠标穿透 · Ctrl + Alt + L 解除" disabled={busy || !connected} onClick={() => void captions.clickThrough(!windowState.click_through)}><MousePointer2 size={15} /></IconButton><IconButton label="停止字幕并关闭" disabled={busy || !connected} onClick={() => void captions.close()}><X size={16} /></IconButton></div>
    <CaptionText state={state} failure={failure || (!connected ? '正在连接字幕服务…' : '')} />
    {windowState.click_through && <span className="caption-unlock-hint">Ctrl + Alt + L 解除穿透</span>}
    <button className="caption-resize" title="拖动调整字幕窗大小" aria-label="调整字幕窗大小" onPointerDown={event => { if (event.button !== 0) return; void getCurrentWindow().startResizeDragging('SouthEast').catch(captions.reportFailure); }} />
  </div>;
}
