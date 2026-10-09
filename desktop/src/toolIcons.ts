import { ClipboardList, AudioLines, BookOpen, Captions, CircleDot, Command, Network, FolderInput, FolderSync, TerminalSquare, House, MemoryStick, MonitorUp, Music2, Puzzle, Radio, ReceiptText, Settings2, ShieldCheck, Smartphone, Speech } from 'lucide-react';
import type { Page } from './context';

// Shared by the home cards and navigation so each tool has one distinct icon.
export const toolIcons = {
  clipboard: ClipboardList,
  home: House,
  'orb-settings': CircleDot,
  ram: MemoryStick,
  relay: FolderInput,
  filesync: FolderSync,
  memory: BookOpen,
  media: AudioLines,
  netease: Music2,
  live: Radio,
  captions: Captions,
  practice: Speech,
  expenses: ReceiptText,
  network: Network,
  shizuku: Smartphone,
  fnconnect: MonitorUp,
  gpu: ShieldCheck,
  codex: Command,
  scripts: TerminalSquare,
  plugins: Puzzle,
  settings: Settings2,
} satisfies Record<Exclude<Page, 'phonetics'>, typeof House>;
