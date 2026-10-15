//! Fast serialization helpers and tool-execution metadata tracking.
//!
//! `serde_json` is already fast; this module adds batch helpers and a
//! lightweight metadata recorder for tool calls (name, duration, status).

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

/// Encode a JSON string value list as a JSON array string.
pub fn encode_str_list(items: &[&str]) -> String {
    serde_json::to_string(items).unwrap_or_else(|_| "[]".to_string())
}

/// Decode a JSON array string into a Vec<String>.
pub fn decode_str_list(s: &str) -> Vec<String> {
    serde_json::from_str(s).unwrap_or_default()
}

/// Validate that `s` is well-formed JSON. Returns (valid, error_message).
pub fn validate_json(s: &str) -> (bool, String) {
    match serde_json::from_str::<serde_json::Value>(s) {
        Ok(_) => (true, String::new()),
        Err(e) => (false, e.to_string()),
    }
}

/// Extract a top-level string field from a JSON object string.
pub fn get_field(json: &str, field: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(json).ok()?;
    v.get(field).and_then(|x| {
        if let Some(s) = x.as_str() {
            Some(s.to_string())
        } else {
            Some(x.to_string())
        }
    })
}

/// Merge two JSON object strings (b wins on conflicts).
pub fn merge_objects(a: &str, b: &str) -> String {
    let mut va: serde_json::Value = serde_json::from_str(a).unwrap_or(serde_json::Value::Null);
    let vb: serde_json::Value = serde_json::from_str(b).unwrap_or(serde_json::Value::Null);
    match (&mut va, vb) {
        (serde_json::Value::Object(ma), serde_json::Value::Object(mb)) => {
            for (k, v) in mb {
                ma.insert(k, v);
            }
            serde_json::to_string(&va).unwrap_or_else(|_| "{}".to_string())
        }
        _ => serde_json::to_string(&va).unwrap_or_else(|_| "null".to_string()),
    }
}

/// One tool execution record.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolRecord {
    pub tool: String,
    pub status: String, // "ok" | "error" | "timeout" | "denied"
    pub duration_ms: u64,
    pub ts_ms: u64,
    pub error: String,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Bounded recorder of tool execution metadata + per-tool aggregates.
#[derive(Debug, Clone)]
pub struct ToolMeta {
    inner: Arc<Mutex<ToolMetaInner>>,
    capacity: usize,
}

#[derive(Debug, Default)]
struct ToolMetaInner {
    records: std::collections::VecDeque<ToolRecord>,
    calls: HashMap<String, u64>,
    errors: HashMap<String, u64>,
    total_duration_ms: HashMap<String, u64>,
}

impl ToolMeta {
    pub fn new(capacity: usize) -> Self {
        ToolMeta {
            inner: Arc::new(Mutex::new(ToolMetaInner::default())),
            capacity: capacity.max(1),
        }
    }

    /// Record one tool execution.
    pub fn record(&self, tool: &str, status: &str, duration_ms: u64, error: &str) {
        let mut inner = self.inner.lock().unwrap();
        if inner.records.len() >= self.capacity {
            inner.records.pop_front();
        }
        inner.records.push_back(ToolRecord {
            tool: tool.to_string(),
            status: status.to_string(),
            duration_ms,
            ts_ms: now_ms(),
            error: error.to_string(),
        });
        *inner.calls.entry(tool.to_string()).or_insert(0) += 1;
        *inner
            .total_duration_ms
            .entry(tool.to_string())
            .or_insert(0) += duration_ms;
        if status != "ok" {
            *inner.errors.entry(tool.to_string()).or_insert(0) += 1;
        }
    }

    /// (calls, errors, avg_duration_ms) for a tool.
    pub fn tool_stats(&self, tool: &str) -> (u64, u64, f64) {
        let inner = self.inner.lock().unwrap();
        let calls = inner.calls.get(tool).copied().unwrap_or(0);
        let errors = inner.errors.get(tool).copied().unwrap_or(0);
        let total = inner.total_duration_ms.get(tool).copied().unwrap_or(0);
        let avg = if calls > 0 { total as f64 / calls as f64 } else { 0.0 };
        (calls, errors, avg)
    }

    /// Slowest tools by average duration (top `n`).
    pub fn slowest(&self, n: usize) -> Vec<(String, f64, u64)> {
        let inner = self.inner.lock().unwrap();
        let mut v: Vec<(String, f64, u64)> = inner
            .calls
            .keys()
            .map(|t| {
                let calls = inner.calls[t];
                let total = inner.total_duration_ms[t];
                (t.clone(), total as f64 / calls.max(1) as f64, calls)
            })
            .collect();
        v.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        v.truncate(n);
        v
    }

    /// Recent records, newest last.
    pub fn recent(&self, n: usize) -> Vec<ToolRecord> {
        let inner = self.inner.lock().unwrap();
        let len = inner.records.len();
        let skip = len.saturating_sub(n);
        inner.records.iter().skip(skip).cloned().collect()
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().records.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_helpers() {
        let enc = encode_str_list(&["a", "b"]);
        assert_eq!(decode_str_list(&enc), vec!["a".to_string(), "b".to_string()]);
        assert!(validate_json("{\"x\":1}").0);
        assert!(!validate_json("{bad").0);
        assert_eq!(get_field("{\"k\":\"v\"}", "k").unwrap(), "v");
        assert_eq!(get_field("{\"k\":42}", "k").unwrap(), "42");
        assert!(get_field("{\"k\":1}", "missing").is_none());
        let m = merge_objects("{\"a\":1}", "{\"b\":2,\"a\":9}");
        let v: serde_json::Value = serde_json::from_str(&m).unwrap();
        assert_eq!(v["a"], 9);
        assert_eq!(v["b"], 2);
    }

    #[test]
    fn tool_meta_records_and_stats() {
        let tm = ToolMeta::new(100);
        tm.record("shell", "ok", 120, "");
        tm.record("shell", "ok", 80, "");
        tm.record("python", "error", 5, "boom");
        let (calls, errors, avg) = tm.tool_stats("shell");
        assert_eq!((calls, errors), (2, 0));
        assert!((avg - 100.0).abs() < 0.01);
        let (c2, e2, _) = tm.tool_stats("python");
        assert_eq!((c2, e2), (1, 1));
        let slow = tm.slowest(1);
        assert_eq!(slow[0].0, "shell");
    }

    #[test]
    fn tool_meta_bounded() {
        let tm = ToolMeta::new(3);
        for i in 0..5 {
            tm.record("t", "ok", i, "");
        }
        assert_eq!(tm.len(), 3);
        let r = tm.recent(10);
        assert_eq!(r[0].duration_ms, 2); // oldest retained
    }

    #[test]
    fn record_json_roundtrip() {
        let tm = ToolMeta::new(10);
        tm.record("x", "timeout", 999, "slow");
        let r = &tm.recent(1)[0];
        let j = serde_json::to_string(r).unwrap();
        let back: ToolRecord = serde_json::from_str(&j).unwrap();
        assert_eq!(back.status, "timeout");
    }
}
