import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText, native, rpc, subscribe, type BackendEvent } from './api';

export type CaptionMode = 'bilingual' | 'translation' | 'original';
export interface CaptionOptions {
  system_id: string; display_mode: CaptionMode; font_size: number;
  background_opacity: number; source_language: string; target_language: string;
}
export interface CaptionSegment {
  id: string; session_id: string; seq: number; start: number; end?: number;
  text: string; translation?: string; final?: boolean; translation_final?: boolean;
  translation_state?: 'pending' | 'ready' | 'failed' | 'disabled'; duration_ms?: number;
}
export interface CaptionState {
  active: boolean; session_id: string | null; status: string; message: string;
  options: CaptionOptions; segments: CaptionSegment[]; revision: number;
}
export interface CaptionWindowState { visible: boolean; click_through: boolean }
export const defaultCaptionState: CaptionState = {
  active: false, session_id: null, status: 'idle', message: '', segments: [], revision: -1,
  options: { system_id: '', display_mode: 'bilingual', font_size: 28, background_opacity: 0.72, source_language: 'auto', target_language: 'zh' },
};

function mergeCue(previous: CaptionSegment | undefined, incoming: CaptionSegment): CaptionSegment {
  if (!previous) return incoming;
  // A final source is immutable. A changed refinal must never attach its new
  // translation to the old source; the backend emits a fresh cue ID instead.
  if (previous.text !== incoming.text || incoming.final === false) return previous;
  if (previous.translation_final && !incoming.translation_final) return previous;
  return { ...previous, ...incoming };
}
function mergeCues(previous: CaptionSegment[], incoming: CaptionSegment[]) {
  const cues = new Map(previous.map(cue => [cue.id, cue]));
  for (const cue of incoming) if (cue.final === true) cues.set(cue.id, mergeCue(cues.get(cue.id), cue));
  return [...cues.values()].sort((a, b) => a.seq - b.seq).slice(-12);
}
export function captionReducer(state: CaptionState, event: BackendEvent): CaptionState {
  const revision = Number(event.revision ?? state.revision);
  if (revision < state.revision) return state;
  if (event.type === 'captions.snapshot') {
    const snapshot = event.state as CaptionState;
    if (snapshot.revision < state.revision) return state;
    const options = { ...defaultCaptionState.options, ...snapshot.options };
    const sameLanguage = options.target_language === state.options.target_language && options.source_language === state.options.source_language;
    const previous = snapshot.session_id === state.session_id && sameLanguage ? state.segments : [];
    return { ...snapshot, options, segments: snapshot.active ? mergeCues(previous, snapshot.segments || []) : [] };
  }
  if (event.type === 'captions.config') {
    const options = { ...state.options, ...event.options as Partial<CaptionOptions> };
    const changedLanguage = options.target_language !== state.options.target_language;
    return { ...state, revision, options, segments: changedLanguage ? state.segments.map(cue => ({ ...cue, translation: '', translation_final: false, translation_state: 'pending' })) : state.segments };
  }
  if (event.type === 'captions.status') {
    const sessionId = event.session_id === undefined ? state.session_id : event.session_id as string | null;
    const active = event.active === undefined ? state.active : Boolean(event.active);
    return { ...state, revision, active, session_id: sessionId, status: String(event.status || state.status), message: String(event.message || ''), segments: !active || sessionId !== state.session_id ? [] : state.segments };
  }
  if (event.type !== 'captions.segment' || !state.active || event.session_id !== state.session_id) return state;
  const cue = event as unknown as CaptionSegment;
  return { ...state, revision, segments: mergeCues(state.segments, [cue]) };
}
export function useCaptions(connected: boolean) {
  const [state, setState] = useState(defaultCaptionState);
  const [windowState, setWindowState] = useState<CaptionWindowState>({ visible: false, click_through: false });
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const receive = useCallback((snapshot: CaptionState) => setState(old => captionReducer(old, { type: 'captions.snapshot', state: snapshot, revision: snapshot.revision })), []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    let off: (() => void) | undefined;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing || disposed) return;
      refreshing = true;
      try { const snapshot = await rpc<CaptionState>('captions.state'); if (!disposed) receive(snapshot); }
      catch (reason) { if (!disposed) setFailure(errorText(reason)); }
      finally { refreshing = false; }
    };
    void subscribe(event => {
      if (disposed) return;
      if (event.type === 'captions.window') {
        setWindowState({ visible: Boolean(event.visible), click_through: Boolean(event.click_through) });
      } else if (event.type.startsWith('captions.')) setState(old => captionReducer(old, event));
    }).then(unlisten => {
      if (disposed) unlisten();
      else { off = unlisten; void refresh(); }
    }).catch(reason => { if (!disposed) setFailure(errorText(reason)); });
    void native.captionState().then(value => { if (!disposed) setWindowState(value); }).catch(reason => { if (!disposed) setFailure(errorText(reason)); });
    const timer = setInterval(() => { void refresh(); }, 3500);
    return () => { disposed = true; off?.(); clearInterval(timer); };
  }, [connected, receive]);
  const perform = useCallback(async (action: () => Promise<void>) => {
    setFailure(''); setBusy(true);
    try { await action(); return true; }
    catch (reason) { if (alive.current) setFailure(errorText(reason)); return false; }
    finally { if (alive.current) setBusy(false); }
  }, []);
  const configure = useCallback(async (options: Partial<CaptionOptions>) => {
    try { receive(await rpc<CaptionState>('captions.configure', options)); setFailure(''); }
    catch (reason) { setFailure(errorText(reason)); }
  }, [receive]);
  const start = useCallback(() => perform(async () => {
    // Open first: a failure to create the window must not leave audio capture
    // running invisibly. The backend reports model/audio startup errors in it.
    await native.captionWindow(true);
    setWindowState(await native.captionState());
    receive(await rpc<CaptionState>('captions.start'));
  }), [perform, receive]);
  const stop = useCallback(() => perform(async () => { receive(await rpc<CaptionState>('captions.stop')); }), [perform, receive]);
  const close = useCallback(() => perform(async () => {
    receive(await rpc<CaptionState>('captions.stop'));
    await native.captionWindow(false);
    setWindowState(await native.captionState());
  }), [perform, receive]);
  const show = useCallback(() => perform(async () => {
    await native.captionWindow(true);
    setWindowState(await native.captionState());
  }), [perform]);
  const clickThrough = useCallback((enabled: boolean) => perform(async () => {
    await native.captionStyle(enabled);
    setWindowState(await native.captionState());
  }), [perform]);
  return { state, windowState, failure, busy, configure, start, stop, close, show, clickThrough, reportFailure: (reason: unknown) => setFailure(errorText(reason)) };
}
