import { createContext, useContext } from 'react';
import type { Job, Settings } from './api';

export type Page = 'home' | 'media' | 'live' | 'captions' | 'practice' | 'phonetics' | 'expenses' | 'shizuku' | 'fnconnect' | 'codex' | 'files' | 'plugins' | 'settings';
export interface AppInfo { name: string; version: string; data_dir: string; ffmpeg?: string | boolean; platform?: string }
export interface AppContextValue {
  page: Page; navigate: (page: Page) => void; connected: boolean; info?: AppInfo;
  setBeforeNavigate?: (guard: (() => boolean | Promise<boolean>) | undefined) => void;
  settings?: Settings; jobs: Job[]; theme: string; setTheme: (value: string) => void;
  refresh: () => Promise<void>; refreshJobs: () => Promise<void>; refreshSettings: () => Promise<void>;
  error: (error: unknown) => void; success: (message: string) => void;
  run: <T>(action: () => Promise<T>, successMessage?: string) => Promise<T | undefined>;
  track: (job: Job, message?: string) => void;
}
export const AppContext = createContext<AppContextValue | null>(null);
export function useApp() { const value = useContext(AppContext); if (!value) throw new Error('App context missing'); return value; }
