export const ORB_MOTION_MS = 260;
export type OrbPoint = { x: number; y: number; menu_x?: number; menu_y?: number };

/** Serialize native region changes; stale completions must not clip a reopened UI. */
export class OrbMotion {
  private revision = 0;
  private disposed = false;
  private tail: Promise<void> = Promise.resolve();
  private timer: ReturnType<typeof setTimeout> | undefined;
  native: (expanded: boolean, focus: boolean) => Promise<OrbPoint>;
  render: (expanded: boolean, layout?: OrbPoint) => void;
  duration: () => number;
  onError: (error: unknown) => void;
  constructor(native: OrbMotion['native'], render: OrbMotion['render'], duration: () => number, onError: OrbMotion['onError']) {
    this.native = native; this.render = render; this.duration = duration; this.onError = onError;
  }
  private enqueue(expanded: boolean, focus: boolean, revision: number) {
    this.tail = this.tail.then(async () => {
      if (this.disposed || revision !== this.revision) return;
      const layout = await this.native(expanded, focus);
      if (!this.disposed && revision === this.revision) this.render(expanded, layout);
    }).catch(error => { if (!this.disposed && revision === this.revision) this.onError(error); });
    return this.tail;
  }
  initialize() { return this.enqueue(false, false, ++this.revision); }
  show(focus = false) {
    clearTimeout(this.timer);
    return this.enqueue(true, focus, ++this.revision);
  }
  hide() {
    clearTimeout(this.timer);
    const revision = ++this.revision;
    this.render(false);
    this.timer = setTimeout(() => void this.enqueue(false, false, revision), this.duration());
  }
  dispose() { this.disposed = true; ++this.revision; clearTimeout(this.timer); }
}
