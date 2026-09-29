export interface OrbProtectionState { x: number; y: number; dim: boolean; asleep: boolean }

/** Sparse timers only; never move the target while the user is interacting. */
export class OrbProtection {
  private timer?: ReturnType<typeof setTimeout>;
  private sleepTimer?: ReturnType<typeof setTimeout>;
  private phase = 0;
  private enabled = false;
  private autoHide = true;
  private mayShift = true;
  private state: OrbProtectionState = { x: 0, y: 0, dim: false, asleep: false };
  constructor(private publish: (state: OrbProtectionState) => void) {}
  idle(enabled: boolean, autoHide = true, mayShift = true) {
    this.dispose(); this.enabled = enabled; this.autoHide = autoHide; this.mayShift = mayShift;
    this.state = { ...this.state, dim: false, asleep: false };
    this.publish(this.state);
    if (!enabled) return;
    this.timer = setTimeout(() => {
      this.state = { ...this.state, dim: true }; this.publish(this.state);
      if (mayShift) this.timer = setTimeout(() => this.shift(), 40_000);
    }, 20_000);
    if (autoHide) this.sleepTimer = setTimeout(() => {
      clearTimeout(this.timer);
      this.state = { ...this.state, dim: true, asleep: true }; this.publish(this.state);
    }, 300_000);
  }
  wake() { this.idle(this.enabled, this.autoHide, this.mayShift); }
  private shift() {
    // Coprime stride visits every point in a 7x7 grid before repeating.
    const point = (this.phase++ * 17 + 27) % 49;
    const x = (point % 7 - 3) * 4, y = (Math.floor(point / 7) - 3) * 4;
    this.state = { x, y, dim: true, asleep: false }; this.publish(this.state);
    this.timer = setTimeout(() => this.shift(), 60_000);
  }
  dispose() { clearTimeout(this.timer); clearTimeout(this.sleepTimer); }
}
