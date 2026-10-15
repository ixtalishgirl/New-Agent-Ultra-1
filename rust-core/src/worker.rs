//! Sandboxed worker coordination: fixed-size worker pool.
//!
//! Workers pull jobs from a shared queue, run a caller-supplied handler,
//! and report results. This module owns the pool mechanics (spawn,
//! dispatch, shutdown, stats); the actual job logic stays in Python or
//! in a Rust callback, keeping the "sandbox boundary" explicit.

use crate::queue::MessageQueue;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

/// A unit of work for the pool.
#[derive(Debug, Clone)]
pub struct Job {
    pub id: String,
    pub kind: String,
    pub payload: String,
}

/// Result produced by a worker.
#[derive(Debug, Clone)]
pub struct JobResult {
    pub job_id: String,
    pub ok: bool,
    pub output: String,
    pub duration_ms: u64,
}

/// Fixed-size worker pool.
pub struct WorkerPool {
    jobs: MessageQueue,
    results: MessageQueue,
    shutdown: Arc<AtomicBool>,
    handles: Vec<JoinHandle<()>>,
    submitted: Arc<AtomicU64>,
    finished: Arc<AtomicU64>,
    failed: Arc<AtomicU64>,
    worker_count: usize,
}

impl WorkerPool {
    /// Create a pool with `workers` threads. `handler` maps a job to
    /// (ok, output). Queue capacity bounds in-flight jobs.
    pub fn new<F>(workers: usize, queue_capacity: usize, handler: F) -> Self
    where
        F: Fn(&Job) -> (bool, String) + Send + Sync + 'static,
    {
        let workers = workers.max(1);
        let jobs = MessageQueue::bounded(queue_capacity.max(workers * 2));
        let results = MessageQueue::unbounded();
        let shutdown = Arc::new(AtomicBool::new(false));
        let submitted = Arc::new(AtomicU64::new(0));
        let finished = Arc::new(AtomicU64::new(0));
        let failed = Arc::new(AtomicU64::new(0));
        let handler = Arc::new(handler);

        let mut handles = Vec::with_capacity(workers);
        for wid in 0..workers {
            let jq = jobs.clone();
            let rq = results.clone();
            let sd = shutdown.clone();
            let h = handler.clone();
            let fin = finished.clone();
            let fail = failed.clone();
            handles.push(thread::spawn(move || {
                let _ = wid;
                while !sd.load(Ordering::Relaxed) {
                    match jq.recv_timeout(100) {
                        Some(msg) => {
                            let job = Job {
                                id: msg.sender.clone(),
                                kind: msg.kind.clone(),
                                payload: msg.body.clone(),
                            };
                            let start = std::time::Instant::now();
                            // Isolate panics per job: a crashing handler must not kill the worker.
                            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                                h(&job)
                            }));
                            let (ok, output) = match outcome {
                                Ok(v) => v,
                                Err(_) => (false, "worker handler panicked".to_string()),
                            };
                            fin.fetch_add(1, Ordering::Relaxed);
                            if !ok {
                                fail.fetch_add(1, Ordering::Relaxed);
                            }
                            let res = JobResult {
                                job_id: job.id,
                                ok,
                                output,
                                duration_ms: start.elapsed().as_millis() as u64,
                            };
                            let body = serde_json::to_string(&res.output).unwrap_or_default();
                            rq.send(
                                if res.ok { "job.ok" } else { "job.error" },
                                &body,
                                &res.job_id,
                            );
                        }
                        None => continue,
                    }
                }
            }));
        }

        WorkerPool {
            jobs,
            results,
            shutdown,
            handles,
            submitted,
            finished,
            failed,
            worker_count: workers,
        }
    }

    /// Submit a job; false when the queue is full.
    pub fn submit(&self, id: &str, kind: &str, payload: &str) -> bool {
        if self.jobs.try_send(kind, payload, id) {
            self.submitted.fetch_add(1, Ordering::Relaxed);
            true
        } else {
            false
        }
    }

    /// Non-blocking result poll. Returns (job_id, ok, output).
    pub fn poll_result(&self) -> Option<(String, bool, String)> {
        self.results.try_recv().map(|m| {
            let ok = m.kind == "job.ok";
            let output: String = serde_json::from_str(&m.body).unwrap_or_default();
            (m.sender, ok, output)
        })
    }

    /// Drain all available results.
    pub fn drain_results(&self) -> Vec<(String, bool, String)> {
        let mut out = Vec::new();
        while let Some(r) = self.poll_result() {
            out.push(r);
        }
        out
    }

    /// Block until `count` results arrive or timeout elapses. Returns results collected.
    pub fn wait_for(&self, count: usize, timeout_ms: u64) -> Vec<(String, bool, String)> {
        let deadline = std::time::Instant::now() + Duration::from_millis(timeout_ms);
        let mut out = Vec::new();
        while out.len() < count && std::time::Instant::now() < deadline {
            if let Some(r) = self.poll_result() {
                out.push(r);
            } else {
                thread::sleep(Duration::from_millis(2));
            }
        }
        out
    }

    /// (submitted, finished, failed, pending_jobs)
    pub fn stats(&self) -> (u64, u64, u64, usize) {
        (
            self.submitted.load(Ordering::Relaxed),
            self.finished.load(Ordering::Relaxed),
            self.failed.load(Ordering::Relaxed),
            self.jobs.len(),
        )
    }

    pub fn worker_count(&self) -> usize {
        self.worker_count
    }

    /// Signal shutdown and join worker threads.
    pub fn shutdown(mut self) {
        self.shutdown.store(true, Ordering::Relaxed);
        for h in self.handles.drain(..) {
            let _ = h.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn echo_pool(workers: usize) -> WorkerPool {
        WorkerPool::new(workers, 64, |job: &Job| {
            (true, format!("echo:{}", job.payload))
        })
    }

    #[test]
    fn submit_and_collect() {
        let pool = echo_pool(2);
        for i in 0..10 {
            assert!(pool.submit(&format!("j{i}"), "echo", &format!("p{i}")));
        }
        let got = pool.wait_for(10, 5000);
        assert_eq!(got.len(), 10);
        let (s, f, failed, pending) = pool.stats();
        assert_eq!((s, f, failed, pending), (10, 10, 0, 0));
        pool.shutdown();
    }

    #[test]
    fn failing_jobs_counted() {
        let pool = WorkerPool::new(2, 64, |job: &Job| {
            if job.payload == "bad" {
                (false, "nope".to_string())
            } else {
                (true, "ok".to_string())
            }
        });
        pool.submit("a", "k", "bad");
        pool.submit("b", "k", "good");
        let got = pool.wait_for(2, 5000);
        assert_eq!(got.len(), 2);
        let (_, _, failed, _) = pool.stats();
        assert_eq!(failed, 1);
        pool.shutdown();
    }

    #[test]
    fn panic_isolated() {
        let pool = WorkerPool::new(1, 64, |job: &Job| {
            if job.payload == "panic" {
                panic!("intentional");
            }
            (true, "fine".to_string())
        });
        pool.submit("p", "k", "panic");
        pool.submit("q", "k", "fine");
        let got = pool.wait_for(2, 5000);
        assert_eq!(got.len(), 2); // worker survived the panic
        pool.shutdown();
    }

    #[test]
    fn backpressure_on_full_queue() {
        let pool = WorkerPool::new(1, 2, |_: &Job| {
            std::thread::sleep(Duration::from_millis(50));
            (true, String::new())
        });
        let mut accepted = 0;
        for i in 0..20 {
            if pool.submit(&format!("j{i}"), "k", "") {
                accepted += 1;
            }
        }
        assert!(accepted < 20); // some rejected: bounded queue
        pool.shutdown();
    }
}
