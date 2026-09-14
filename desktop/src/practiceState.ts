import type { PracticeItem } from './practicePlayer';

export interface PracticeDraft { text: string; voice: string; folder_id: string | null }
export const emptyPracticeDraft = (): PracticeDraft => ({ text: '', voice: 'Cherry', folder_id: null });
export function practiceDraft(item: PracticeItem): PracticeDraft { return { text: item.text, voice: item.voice || 'Cherry', folder_id: item.folder_id || null }; }
export function practiceDirty(draft: PracticeDraft, item?: PracticeItem): boolean {
  if (!item) return !!draft.text.trim();
  return draft.text !== item.text || draft.voice !== item.voice || (draft.folder_id || null) !== (item.folder_id || null);
}
export function practicePlayable(item?: PracticeItem): boolean {
  return !!item?.sentences?.length && !item.stale && (!item.status || item.status === 'ready') && item.sentences.every(sentence => !!sentence.audio_id);
}
export function filterPracticeItems(items: PracticeItem[], folder: string): PracticeItem[] {
  if (folder === 'all') return items;
  return items.filter(item => (item.folder_id || 'unfiled') === folder);
}
export function acceptPracticeCompletion(parentId: string, selectedId: string | undefined, dirty: boolean): boolean {
  return parentId === selectedId && !dirty;
}
