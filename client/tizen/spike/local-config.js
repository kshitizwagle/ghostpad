// Spike settings. Committed with empty values; client/tizen/package_spike.py writes the real
// values into the packaged copy only. Empty pcHost: use the last PC that answered, if any.
// autoConnect: after another client takes over, reconnect once it leaves (never steals back).
window.GHOSTPAD_SPIKE_CONFIG = { pcHost: '', pcPort: 47800, token: '', autoConnect: true };
