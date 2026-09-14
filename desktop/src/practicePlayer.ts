export interface PracticeSentence { text: string; audio_id: string; duration?: number }
export interface PracticeItem {
  id: string; text: string; voice: string; model: string; created_at: number;
  sentences: PracticeSentence[]; path?: string; reused?: number; audio_id?: string;
  revision?: number; folder_id?: string | null; updated_at?: number;
  status?: 'draft' | 'ready' | 'stale' | 'generating' | 'error'; stale?: boolean;
  fragments?: PracticeFragment[];
}
export interface PracticeFragment extends PracticeItem { parent_id: string; parent_revision?: number }
export interface PracticeAudio { data: string; mime: string }
export interface PracticePlayback { index: number; playing: boolean; loading: boolean; time: number; duration: number }
export type PracticePlayMode = 'continuous' | 'repeat';
export const emptyPlayback: PracticePlayback = { index: 0, playing: false, loading: false, time: 0, duration: 0 };

export function practiceTokens(text: string): { text: string; word?: string }[] {
  return text.split(/([A-Za-z]+(?:['’\-][A-Za-z]+)*)/).filter(Boolean).map(token => /^[A-Za-z]/.test(token) ? { text: token, word: token } : { text: token });
}

export function audioBlobUrl(payload: PracticeAudio): string {
  const bytes = Uint8Array.from(atob(payload.data), value => value.charCodeAt(0));
  return URL.createObjectURL(new Blob([bytes], { type: payload.mime || 'audio/wav' }));
}

// Own one audio element and one object URL. Requests can finish out of order;
// only the most recent request may attach or play an audio source.
export class PracticePlayer {
  private item?: PracticeItem;
  private state: PracticePlayback = { ...emptyPlayback };
  private mode: PracticePlayMode = 'continuous';
  private rate = 1;
  private url?: string;
  private audioId?: string;
  private request = 0;
  private disposed = false;
  private listeners: [string, EventListener][] = [];
  constructor(
    private audio: HTMLAudioElement,
    private fetchAudio: (id: string) => Promise<PracticeAudio>,
    private changed: (state: PracticePlayback) => void,
    private failed: (error: unknown) => void,
    private makeUrl: (payload: PracticeAudio) => string = audioBlobUrl,
    private revokeUrl: (url: string) => void = url => URL.revokeObjectURL(url),
  ) {
    const listen = (name: string, callback: () => void) => {
      const listener: EventListener = callback;
      this.listeners.push([name, listener]); this.audio.addEventListener(name, listener);
    };
    listen('play', () => this.update({ playing: true, loading: false }));
    listen('pause', () => this.update({ playing: false }));
    listen('timeupdate', () => this.update({ time: this.audio.currentTime }));
    listen('loadedmetadata', () => this.update({ duration: Number.isFinite(this.audio.duration) ? this.audio.duration : 0 }));
    listen('ended', () => {
      this.update({ playing: false });
      if (this.mode === 'repeat') void this.play(this.state.index, true);
      else if (this.item && this.state.index + 1 < this.item.sentences.length) void this.play(this.state.index + 1, true);
    });
    listen('error', () => { if (this.url && !this.disposed) { this.update({ playing: false, loading: false }); this.failed(new Error('缓存音频无法播放，请重新生成这段练习。')); } });
  }
  private update(patch: Partial<PracticePlayback>) { if (!this.disposed) { this.state = { ...this.state, ...patch }; this.changed({ ...this.state }); } }
  private release() {
    this.audioId = undefined;
    const previous = this.url; this.url = undefined;
    this.audio.removeAttribute('src'); this.audio.load();
    if (previous) this.revokeUrl(previous);
  }
  setItem(item?: PracticeItem) {
    this.request++; this.audio.pause(); this.release(); this.item = item;
    this.update({ ...emptyPlayback, duration: item?.sentences[0]?.duration || 0 });
  }
  setMode(mode: PracticePlayMode) { this.mode = mode; }
  setRate(rate: number) { this.rate = rate; this.audio.playbackRate = rate; }
  pause() { this.request++; this.audio.pause(); this.update({ playing: false, loading: false }); }
  seek(time: number) { if (this.url) { this.audio.currentTime = Math.max(0, Math.min(time, this.audio.duration || 0)); this.update({ time: this.audio.currentTime }); } }
  async play(index = this.state.index, restart = false) {
    const sentence = this.item?.sentences[index];
    if (!sentence || this.disposed) return;
    const token = ++this.request;
    this.audio.pause();
    this.update({ index, loading: true, playing: false, ...(this.audioId !== sentence.audio_id ? { time: 0, duration: sentence.duration || 0 } : {}) });
    try {
      if (this.audioId !== sentence.audio_id || !this.url) {
        const payload = await this.fetchAudio(sentence.audio_id);
        if (this.disposed || token !== this.request) return;
        this.release(); this.url = this.makeUrl(payload); this.audioId = sentence.audio_id; this.audio.src = this.url;
      }
      if (this.disposed || token !== this.request) return;
      if (restart || this.audio.ended) this.audio.currentTime = 0;
      this.audio.playbackRate = this.rate;
      await this.audio.play();
      if (!this.disposed && token === this.request) this.update({ loading: false, playing: true });
    } catch (reason) { if (!this.disposed && token === this.request) { this.update({ loading: false, playing: false }); this.failed(reason); } }
  }
  dispose() {
    this.disposed = true; this.request++;
    for (const [name, listener] of this.listeners) this.audio.removeEventListener(name, listener);
    this.audio.pause(); this.release();
  }
}
