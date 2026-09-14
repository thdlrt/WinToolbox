import type { CaptionMode, CaptionSegment } from './captions';

export interface CaptionPage { original: string; translation: string; cueId: string; seq: number; warning: string }
export interface CaptionLayout { width: number; originalLines: number; translationLines: number; originalMeasure: (value: string) => number; translationMeasure: (value: string) => number; key: string }
export interface CaptionFrame { page?: CaptionPage; pageIndex: number; pageCount: number; skipped: boolean; waiting: boolean }

// Wrap from the beginning, retaining every token. Only whitespace is normalized.
export function wrapCaptionText(text: string, width: number, measure: (value: string) => number): string[] {
  if (width <= 0) return [];
  const clean = text.replace(/\s+/gu, ' ').trim();
  if (!clean) return [];
  const tokens = (clean.match(/[\p{L}\p{N}]+(?:['’\-][\p{L}\p{N}]+)*|\s+|[^\s]/gu) || [])
    .flatMap(token => measure(token) > width ? Array.from(token) : [token]);
  const lines: string[] = []; let line = '';
  for (const token of tokens) {
    const next = (line + token).trimStart();
    if (line.trim() && measure(next.trimEnd()) > width) { lines.push(line.trim()); line = token.trimStart(); }
    else line = next;
  }
  if (line.trim()) lines.push(line.trim());
  // Avoid a final screen containing only one short English word. Move whole
  // words from the preceding line without losing text or exceeding the width.
  if (lines.length > 1 && measure(lines[lines.length - 1]) < width * .35) {
    const words = lines[lines.length - 2].split(' ');
    while (words.length > 2 && measure(lines[lines.length - 1]) < width * .6) {
      const candidate = `${words[words.length - 1]} ${lines[lines.length - 1]}`;
      if (measure(candidate) > width || measure(words.slice(0, -1).join(' ')) < measure(candidate)) break;
      words.pop(); lines[lines.length - 1] = candidate;
    }
    lines[lines.length - 2] = words.join(' ');
  }
  // Keep closing CJK punctuation with a preceding character rather than
  // starting a new page with a comma or full stop.
  for (let index = 1; index < lines.length; index++) {
    if (!/^[，。！？；：、）】》」』]/u.test(lines[index])) continue;
    const previous = Array.from(lines[index - 1]);
    if (previous.length > 1 && measure(previous[previous.length - 1] + lines[index]) <= width) {
      lines[index] = previous.pop()! + lines[index]; lines[index - 1] = previous.join('');
    }
  }
  return lines;
}

export function captionPages(cue: CaptionSegment, mode: CaptionMode, layout: CaptionLayout, fallback = false): CaptionPage[] {
  const original = mode !== 'translation' || fallback ? wrapCaptionText(cue.text, layout.width, layout.originalMeasure) : [];
  const translation = mode !== 'original' && !fallback ? wrapCaptionText(cue.translation || '', layout.width, layout.translationMeasure) : [];
  const count = Math.max(1, Math.ceil(original.length / Math.max(1, layout.originalLines)), Math.ceil(translation.length / Math.max(1, layout.translationLines)));
  const column = (lines: string[], index: number) => {
    if (!lines.length) return '';
    const start = Math.floor(index * lines.length / count), end = Math.floor((index + 1) * lines.length / count);
    return lines.slice(start, Math.max(start + 1, end)).join('\n');
  };
  // Both columns are divided across the same page count of one cue. We never
  // substitute another cue's translation or discard the start/end of text.
  return Array.from({ length: count }, (_, index) => ({
    original: column(original, index),
    translation: column(translation, index),
    cueId: cue.id, seq: cue.seq, warning: fallback ? cue.translation_state === 'failed' ? '翻译失败，显示原文' : '译文未就绪，显示原文' : '',
  }));
}

export function captionReadMs(page: Pick<CaptionPage, 'original' | 'translation'>, suggested = 0) {
  const text = page.translation || page.original;
  const cjk = (text.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/gu) || []).length;
  const words = (text.match(/[A-Za-zÀ-ÿ0-9]+/gu) || []).length;
  return Math.max(2000, Math.min(5000, Math.max(suggested, 900 + cjk * 95 + words * 230)));
}

interface PendingCue { cue: CaptionSegment; arrived: number }
interface PlayingCue { cue: CaptionSegment; pages: CaptionPage[]; index: number; until: number; fallback: boolean }

export class CaptionPager {
  private context = '';
  private mode: CaptionMode = 'bilingual';
  private target = '';
  private layout?: CaptionLayout;
  private pending: PendingCue[] = [];
  private playing?: PlayingCue;
  private consumed = -1;
  private skippedUntil = 0;
  private inactive = true;

  update(session: string | null, active: boolean, mode: CaptionMode, target: string, cues: CaptionSegment[], layout: CaptionLayout, now: number) {
    const sessionKey = session || '';
    if (!active || sessionKey !== this.context || this.inactive) {
      this.context = sessionKey; this.pending = []; this.playing = undefined; this.consumed = -1; this.skippedUntil = 0;
    }
    this.inactive = !active;
    if (!active) { this.mode = mode; this.target = target; this.layout = layout; return this.frame(now); }
    if (this.target && this.target !== target) {
      this.consumed = this.playing ? this.playing.cue.seq - 1 : this.consumed;
      this.playing = undefined; this.pending = [];
    } else if (this.mode !== mode && this.playing) {
      const selected = cues.find(cue => cue.id === this.playing!.cue.id) || this.playing.cue;
      this.consumed = selected.seq - 1; this.pending.unshift({ cue: selected, arrived: now }); this.playing = undefined;
    }
    this.mode = mode; this.target = target;
    if (this.playing && this.layout?.key !== layout.key) {
      this.playing.pages = captionPages(this.playing.cue, mode, layout, this.playing.fallback);
      this.playing.index = 0; this.playing.until = now + this.duration(this.playing.pages[0]);
    }
    this.layout = layout;
    for (const cue of cues) {
      if (cue.final !== true || cue.seq <= this.consumed || cue.id === this.playing?.cue.id) continue;
      const pending = this.pending.find(value => value.cue.id === cue.id);
      if (pending) pending.cue = cue; else this.pending.push({ cue, arrived: now });
    }
    this.pending.sort((a, b) => a.cue.seq - b.cue.seq);
    // Bound waiting latency. The current cue finishes every page; catch-up
    // skips entire queued cues instead of cutting the current sentence.
    return this.tick(now);
  }

  private duration(page: CaptionPage) { return captionReadMs(page); }
  private trimQueue(now: number) {
    if (!this.layout) return;
    const cost = (entry: PendingCue) => {
      const ready = !!entry.cue.translation?.trim() && (entry.cue.translation_state === 'ready' || entry.cue.translation_final === true);
      return captionPages(entry.cue, this.mode, this.layout!, this.mode !== 'original' && !ready).reduce((total, page) => total + this.duration(page), 0);
    };
    while (this.pending.length > 1 && (this.pending.length > 6 || this.pending.reduce((sum, entry) => sum + cost(entry), 0) > 12000 || now - this.pending[0].arrived > 12000)) {
      const removed = this.pending.shift()!; this.consumed = Math.max(this.consumed, removed.cue.seq); this.skippedUntil = now + 3500;
    }
  }
  tick(now: number): CaptionFrame {
    if (this.inactive || !this.layout) return this.frame(now);
    this.trimQueue(now);
    const current = this.playing;
    if (current && now >= current.until && current.index < current.pages.length - 1) {
      current.index++; current.until = now + this.duration(current.pages[current.index]);
    }
    if (!current || current.index === current.pages.length - 1 && now >= current.until) {
      const next = this.pending[0];
      if (next) {
        const ready = !!next.cue.translation?.trim() && (next.cue.translation_state === 'ready' || next.cue.translation_final === true);
        const fallback = this.mode !== 'original' && !ready;
        if (!fallback || next.cue.translation_state === 'failed' || now - next.arrived >= 3000) {
          this.pending.shift(); const cue = { ...next.cue };
          const pages = captionPages(cue, this.mode, this.layout, fallback);
          this.playing = { cue, pages, index: 0, until: now + this.duration(pages[0]), fallback };
          this.consumed = Math.max(this.consumed, cue.seq);
        }
      }
    }
    return this.frame(now);
  }
  private frame(now: number): CaptionFrame {
    const playing = this.playing;
    return { page: playing?.pages[playing.index], pageIndex: playing?.index || 0, pageCount: playing?.pages.length || 0, skipped: now < this.skippedUntil, waiting: !playing && this.pending.length > 0 };
  }
}
