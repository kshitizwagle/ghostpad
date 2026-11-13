// STATE send policy helpers (DESIGN.md "Send policy"). Pure, DOM-free.

export const HEARTBEAT_MS = 50;
// Polls are quantised to the scheduler tick (16.7 ms on 60 Hz rAF). A strict
// `elapsed < 50` check misses the tick at 49.9 ms and slips to 66.7 ms (issue #4).
// Sending when within half a 60 Hz frame of the deadline keeps the cadence at ~50 ms.
export const HEARTBEAT_SLACK_MS = 8;

/** True when an unchanged pad is due a heartbeat. */
export function heartbeatDue(now, lastSendAt) {
  return now - lastSendAt >= HEARTBEAT_MS - HEARTBEAT_SLACK_MS;
}
