//! Token-bucket rate limiter and request accounting.
//!
//! Used to bound tool execution rates, API calls, and internal
//! request flows. Thread-safe; cheap enough to check per request.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Single token bucket.
#[derive(Debug)]
struct Bucket {
    capacity: f64,
    tokens: f64,
    refill_per_sec: f64,
    last: Instant,
}

impl Bucket {
    fn new(capacity: f64, refill_per_sec: f64) -> Self {
        Bucket {
            capacity,
            tokens: capacity,
            refill_per_sec,
            last: Instant::now(),
        }
    }

    fn refill(&mut self) {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.refill_per_sec).min(self.capacity);
        self.last = now;
    }

    /// Try to consume `n` tokens. Returns (allowed, retry_after_secs).
    fn try_take(&mut self, n: f64) -> (bool, f64) {
        self.refill();
        if self.tokens >= n {
            self.tokens -= n;
            (true, 0.0)
        } else {
            let needed = n - self.tokens;
            let wait = if self.refill_per_sec > 0.0 {
                needed / self.refill_per_sec
            } else {
                f64::INFINITY
            };
            (false, wait)
        }
    }
}

/// Named rate limiter holding one bucket per key.
#[derive(Debug, Clone)]
pub struct RateLimiter {
    inner: Arc<Mutex<RateLimiterInner>>,
    default_capacity: f64,
    default_refill_per_sec: f64,
}

#[derive(Debug, Default)]
struct RateLimiterInner {
    buckets: HashMap<String, Bucket>,
    allowed: u64,
    denied: u64,
}

impl RateLimiter {
    /// Create a limiter; each new key gets `capacity` tokens refilling at
    /// `refill_per_sec`.
    pub fn new(capacity: f64, refill_per_sec: f64) -> Self {
        RateLimiter {
            inner: Arc::new(Mutex::new(RateLimiterInner::default())),
            default_capacity: capacity,
            default_refill_per_sec: refill_per_sec,
        }
    }

    /// Check (and consume) one token for `key`. Returns (allowed, retry_after_secs).
    pub fn check(&self, key: &str) -> (bool, f64) {
        self.check_n(key, 1.0)
    }

    /// Check (and consume) `n` tokens for `key`.
    pub fn check_n(&self, key: &str, n: f64) -> (bool, f64) {
        let mut inner = self.inner.lock().unwrap();
        let bucket = inner.buckets.entry(key.to_string()).or_insert_with(|| {
            Bucket::new(self.default_capacity, self.default_refill_per_sec)
        });
        let (ok, wait) = bucket.try_take(n);
        if ok {
            inner.allowed += 1;
        } else {
            inner.denied += 1;
        }
        (ok, wait)
    }

    /// Reset the bucket for `key` (e.g. after a config change).
    pub fn reset(&self, key: &str) {
        let mut inner = self.inner.lock().unwrap();
        inner.buckets.remove(key);
    }

    /// (allowed_total, denied_total, tracked_keys)
    pub fn stats(&self) -> (u64, u64, usize) {
        let inner = self.inner.lock().unwrap();
        (inner.allowed, inner.denied, inner.buckets.len())
    }
}

/// Sliding-window request counter: counts requests per key in the last
/// `window_secs` seconds. Useful for "N requests per minute" policies.
#[derive(Debug, Clone)]
pub struct SlidingWindow {
    inner: Arc<Mutex<HashMap<String, Vec<Instant>>>>,
    window: Duration,
}

impl SlidingWindow {
    pub fn new(window_secs: u64) -> Self {
        SlidingWindow {
            inner: Arc::new(Mutex::new(HashMap::new())),
            window: Duration::from_secs(window_secs.max(1)),
        }
    }

    /// Record a request for `key`; returns the count within the window (including this one).
    pub fn record(&self, key: &str) -> usize {
        let mut inner = self.inner.lock().unwrap();
        let now = Instant::now();
        let entries = inner.entry(key.to_string()).or_default();
        entries.push(now);
        let cutoff = now - self.window;
        entries.retain(|t| *t > cutoff);
        entries.len()
    }

    /// Current count for `key` without recording.
    pub fn count(&self, key: &str) -> usize {
        let mut inner = self.inner.lock().unwrap();
        let now = Instant::now();
        match inner.get_mut(key) {
            Some(entries) => {
                let cutoff = now - self.window;
                entries.retain(|t| *t > cutoff);
                entries.len()
            }
            None => 0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_bucket_allows_then_denies() {
        let rl = RateLimiter::new(3.0, 1.0);
        assert!(rl.check("api").0);
        assert!(rl.check("api").0);
        assert!(rl.check("api").0);
        let (ok, wait) = rl.check("api");
        assert!(!ok);
        assert!(wait > 0.0 && wait <= 1.1);
        let (allowed, denied, _) = rl.stats();
        assert_eq!((allowed, denied), (3, 1));
    }

    #[test]
    fn buckets_are_per_key() {
        let rl = RateLimiter::new(1.0, 0.0);
        assert!(rl.check("a").0);
        assert!(!rl.check("a").0);
        assert!(rl.check("b").0); // independent key
    }

    #[test]
    fn refill_over_time() {
        let rl = RateLimiter::new(1.0, 10.0);
        assert!(rl.check("k").0);
        assert!(!rl.check("k").0);
        std::thread::sleep(Duration::from_millis(150));
        assert!(rl.check("k").0);
    }

    #[test]
    fn reset_restores_capacity() {
        let rl = RateLimiter::new(1.0, 0.0);
        assert!(rl.check("k").0);
        assert!(!rl.check("k").0);
        rl.reset("k");
        assert!(rl.check("k").0);
    }

    #[test]
    fn sliding_window_counts() {
        let sw = SlidingWindow::new(60);
        assert_eq!(sw.record("u"), 1);
        assert_eq!(sw.record("u"), 2);
        assert_eq!(sw.count("u"), 2);
        assert_eq!(sw.count("other"), 0);
    }

    #[test]
    fn concurrent_checks() {
        use std::thread;
        let rl = RateLimiter::new(10_000.0, 10_000.0);
        let mut hs = vec![];
        for _ in 0..8 {
            let r = rl.clone();
            hs.push(thread::spawn(move || {
                for _ in 0..1000 {
                    assert!(r.check("shared").0);
                }
            }));
        }
        for h in hs {
            h.join().unwrap();
        }
        let (allowed, denied, _) = rl.stats();
        assert_eq!(allowed, 8000);
        assert_eq!(denied, 0);
    }
}
