// RUMBLE (server -> client) playback. DOM-free: gamepad lookup and timers are injected.
// The daemon sends rumble state *changes*, not durations, so while the last received
// magnitudes for a pad are non-zero the effect is re-issued every REISSUE_MS with a
// duration that overlaps the next issue. Zero magnitudes (or stop()) end it.

export const REISSUE_MS = 150;
export const EFFECT_MS = 200;

export class RumbleController {
  /**
   * @param {object} o
   * @param {(pad:number)=>object|null} o.getGamepad  returns the live Gamepad for a pad index
   * @param {(pad:number, large:number, small:number)=>void} [o.onChange]  UI hook, 0..255 values
   * @param {Function} [o.setInterval] @param {Function} [o.clearInterval]
   */
  constructor({ getGamepad, onChange = () => {}, setInterval: si, clearInterval: ci, reissueMs = REISSUE_MS } = {}) {
    this.getGamepad = getGamepad;
    this.onChange = onChange;
    this._si = si || globalThis.setInterval.bind(globalThis);
    this._ci = ci || globalThis.clearInterval.bind(globalThis);
    this.reissueMs = reissueMs;
    /** pad -> {large, small} for pads with non-zero magnitudes */
    this.active = new Map();
    this._timer = null;
  }

  /** Handle a decoded RUMBLE message {pad, large, small}. */
  onRumble(msg) {
    if (!msg) return;
    const { pad, large, small } = msg;
    if (large === 0 && small === 0) {
      if (this.active.delete(pad)) this._halt(pad);
      this.onChange(pad, 0, 0);
    } else {
      this.active.set(pad, { large, small });
      this._play(pad, large, small);
      this.onChange(pad, large, small);
    }
    this._syncTimer();
  }

  /** Stop everything (disconnect, page hidden). Idempotent. */
  stop() {
    const pads = [...this.active.keys()];
    this.active.clear();
    this._syncTimer();
    for (const pad of pads) {
      this._halt(pad);
      this.onChange(pad, 0, 0);
    }
  }

  _tick() {
    for (const [pad, m] of this.active) this._play(pad, m.large, m.small);
  }

  _syncTimer() {
    if (this.active.size > 0 && this._timer === null) {
      this._timer = this._si(() => this._tick(), this.reissueMs);
    } else if (this.active.size === 0 && this._timer !== null) {
      this._ci(this._timer);
      this._timer = null;
    }
  }

  _actuator(pad) {
    try {
      const gp = this.getGamepad(pad);
      const a = gp && gp.vibrationActuator;
      return a || null;
    } catch {
      return null;
    }
  }

  _play(pad, large, small) {
    const a = this._actuator(pad);
    if (!a || typeof a.playEffect !== 'function') return;
    try {
      const r = a.playEffect('dual-rumble', {
        startDelay: 0, duration: EFFECT_MS, strongMagnitude: large / 255, weakMagnitude: small / 255,
      });
      if (r && typeof r.catch === 'function') r.catch(() => {});
    } catch { /* unsupported effect type or gamepad gone */ }
  }

  _halt(pad) {
    const a = this._actuator(pad);
    if (!a) return;
    try {
      let r;
      if (typeof a.reset === 'function') r = a.reset();
      else if (typeof a.playEffect === 'function') {
        r = a.playEffect('dual-rumble', { startDelay: 0, duration: 1, strongMagnitude: 0, weakMagnitude: 0 });
      }
      if (r && typeof r.catch === 'function') r.catch(() => {});
    } catch { /* ignore */ }
  }
}
