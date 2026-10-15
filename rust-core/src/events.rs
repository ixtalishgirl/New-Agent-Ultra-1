//! Structured event processing for the agent event bus.
//!
//! Typed events with nanosecond timestamps, in-memory ring buffer,
//! kind-based filtering, and JSON export for the Python side.

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

fn now_nanos() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

/// A structured event emitted by agent subsystems.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    /// Event kind, e.g. "task.started", "tool.executed", "error".
    pub kind: String,
    /// Source subsystem, e.g. "scheduler", "tools".
    pub source: String,
    /// Severity: debug|info|warn|error|critical.
    pub level: String,
    /// Human-readable message.
    pub message: String,
    /// JSON-encoded structured fields.
    pub fields_json: String,
    /// Nanoseconds since UNIX epoch.
    pub ts_nanos: u64,
    /// Monotonic sequence number.
    pub seq: u64,
}

impl Event {
    pub fn new(kind: &str, source: &str, level: &str, message: &str, fields_json: &str, seq: u64) -> Self {
        Event {
            kind: kind.to_string(),
            source: source.to_string(),
            level: level.to_string(),
            message: message.to_string(),
            fields_json: fields_json.to_string(),
            ts_nanos: now_nanos(),
            seq,
        }
    }
}

/// Severity rank for level filtering.
fn level_rank(level: &str) -> u8 {
    match level {
        "debug" => 0,
        "info" => 1,
        "warn" => 2,
        "error" => 3,
        "critical" => 4,
        _ => 1,
    }
}

/// Bounded ring-buffer event log with filtering.
#[derive(Debug, Clone)]
pub struct EventLog {
    inner: Arc<Mutex<EventLogInner>>,
    capacity: usize,
}

#[derive(Debug)]
struct EventLogInner {
    buf: VecDeque<Event>,
    next_seq: u64,
    dropped: u64,
}

impl EventLog {
    /// Create a log holding at most `capacity` recent events.
    pub fn new(capacity: usize) -> Self {
        EventLog {
            inner: Arc::new(Mutex::new(EventLogInner {
                buf: VecDeque::with_capacity(capacity.min(1_000_000)),
                next_seq: 0,
                dropped: 0,
            })),
            capacity: capacity.max(1),
        }
    }

    /// Emit an event. Returns its sequence number.
    pub fn emit(&self, kind: &str, source: &str, level: &str, message: &str, fields_json: &str) -> u64 {
        let mut inner = self.inner.lock().unwrap();
        let seq = inner.next_seq;
        inner.next_seq += 1;
        if inner.buf.len() >= self.capacity {
            inner.buf.pop_front();
            inner.dropped += 1;
        }
        inner.buf.push_back(Event::new(kind, source, level, message, fields_json, seq));
        seq
    }

    /// Number of retained events.
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().buf.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Events dropped due to capacity overflow.
    pub fn dropped(&self) -> u64 {
        self.inner.lock().unwrap().dropped
    }

    /// Filter retained events. `kind_prefix`: match kinds starting with it ("" = all).
    /// `min_level`: minimum severity ("debug".."critical").
    /// `limit`: max events returned (0 = all), newest last.
    pub fn query(&self, kind_prefix: &str, min_level: &str, limit: usize) -> Vec<Event> {
        let inner = self.inner.lock().unwrap();
        let min_rank = level_rank(min_level);
        let mut out: Vec<Event> = inner
            .buf
            .iter()
            .filter(|e| e.kind.starts_with(kind_prefix) && level_rank(&e.level) >= min_rank)
            .cloned()
            .collect();
        if limit > 0 && out.len() > limit {
            let skip = out.len() - limit;
            out.drain(..skip);
        }
        out
    }

    /// Count events by kind prefix (cheap stats without cloning).
    pub fn count(&self, kind_prefix: &str) -> usize {
        self.inner
            .lock()
            .unwrap()
            .buf
            .iter()
            .filter(|e| e.kind.starts_with(kind_prefix))
            .count()
    }

    /// Clear all retained events.
    pub fn clear(&self) {
        self.inner.lock().unwrap().buf.clear();
    }
}

/// Aggregate per-kind counts over a slice of events (for dashboards).
pub fn summarize(events: &[Event]) -> Vec<(String, usize)> {
    use std::collections::HashMap;
    let mut map: HashMap<&str, usize> = HashMap::new();
    for e in events {
        *map.entry(e.kind.as_str()).or_insert(0) += 1;
    }
    let mut v: Vec<(String, usize)> = map.into_iter().map(|(k, c)| (k.to_string(), c)).collect();
    v.sort_by(|a, b| b.1.cmp(&a.1));
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emit_and_query() {
        let log = EventLog::new(100);
        log.emit("task.started", "scheduler", "info", "t1 started", "{}");
        log.emit("task.done", "scheduler", "info", "t1 done", "{}");
        log.emit("tool.error", "tools", "error", "boom", "{}");
        assert_eq!(log.len(), 3);
        assert_eq!(log.query("task.", "debug", 0).len(), 2);
        assert_eq!(log.query("", "error", 0).len(), 1);
        assert_eq!(log.count("task."), 2);
    }

    #[test]
    fn ring_overflow_drops_oldest() {
        let log = EventLog::new(3);
        for i in 0..5 {
            log.emit("e", "s", "info", &format!("m{i}"), "{}");
        }
        assert_eq!(log.len(), 3);
        assert_eq!(log.dropped(), 2);
        let evts = log.query("", "debug", 0);
        assert_eq!(evts[0].message, "m2"); // oldest retained
        assert_eq!(evts[2].message, "m4");
    }

    #[test]
    fn level_filtering() {
        let log = EventLog::new(10);
        log.emit("a", "s", "debug", "d", "{}");
        log.emit("a", "s", "warn", "w", "{}");
        log.emit("a", "s", "critical", "c", "{}");
        assert_eq!(log.query("", "warn", 0).len(), 2);
        assert_eq!(log.query("", "critical", 0).len(), 1);
    }

    #[test]
    fn limit_returns_newest() {
        let log = EventLog::new(10);
        for i in 0..5 {
            log.emit("a", "s", "info", &format!("m{i}"), "{}");
        }
        let got = log.query("", "debug", 2);
        assert_eq!(got.len(), 2);
        assert_eq!(got[1].message, "m4");
    }

    #[test]
    fn summarize_counts() {
        let log = EventLog::new(10);
        log.emit("x", "s", "info", "", "{}");
        log.emit("x", "s", "info", "", "{}");
        log.emit("y", "s", "info", "", "{}");
        let s = summarize(&log.query("", "debug", 0));
        assert_eq!(s[0], ("x".to_string(), 2));
    }

    #[test]
    fn event_json_roundtrip() {
        let log = EventLog::new(10);
        log.emit("k", "src", "info", "msg", "{\"a\":1}");
        let e = &log.query("", "debug", 0)[0];
        let j = serde_json::to_string(e).unwrap();
        let back: Event = serde_json::from_str(&j).unwrap();
        assert_eq!(back.kind, "k");
        assert_eq!(back.fields_json, "{\"a\":1}");
    }
}
