import type { QuickSnapshot } from './quickMenuApi';

export type QuickTranslationSource = 'selection' | 'clipboard';

/** Attempt each source once per text; failed automatic calls wait for an explicit retry. */
export function nextQuickTranslation(snapshot: QuickSnapshot, attempted: ReadonlySet<string>) {
  if (!snapshot.settings.auto_translate || snapshot.initial_job) return;
  const sources: { source: QuickTranslationSource; text: string; eligible: boolean; ready: boolean }[] = [
    { source: 'selection', text: snapshot.text, eligible: snapshot.foreign, ready: !!snapshot.translation },
    { source: 'clipboard', text: snapshot.clipboard_text || '', eligible: !!snapshot.clipboard_foreign, ready: !!snapshot.clipboard_translation },
  ];
  for (const item of sources) {
    const key = `${item.source}:${item.text}`;
    if (item.eligible && !item.ready && !attempted.has(key)) return { source: item.source, key };
  }
}
