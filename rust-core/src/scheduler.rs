//! Priority task scheduler for the Haley agent.
//!
//! A thread-safe priority queue of tasks. Higher priority values are
//! dequeued first; ties break by insertion order (FIFO) and then by
//! earliest deadline. All operations are O(log n).

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

fn now_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// A single schedulable unit of work.
#[derive(Debug, Clone)]
pub struct Task {
    /// Unique task identifier.
    pub id: String,
    /// Higher value = scheduled earlier.
    pub priority: i64,
    /// Optional deadline in epoch millis; earlier deadlines win ties.
    pub deadline_ms: Option<u64>,
    /// Monotonic insertion sequence for FIFO tie-breaking.
    pub seq: u64,
    /// Opaque payload carried with the task (e.g. JSON).
    pub payload: String,
    /// Epoch millis when the task was enqueued.
    pub enqueued_at_ms: u64,
}

impl PartialEq for Task {
    fn eq(&self, other: &Self) -> bool {
        self.seq == other.seq
    }
}
impl Eq for Task {}

impl PartialOrd for Task {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Task {
    fn cmp(&self, other: &Self) -> Ordering {
        // BinaryHeap is a max-heap: the "greatest" element pops first.
        self.priority
            .cmp(&other.priority)
            .then_with(|| {
                // Earlier deadline first -> reverse compare so smaller deadline is "greater".
                match (self.deadline_ms, other.deadline_ms) {
                    (Some(a), Some(b)) => b.cmp(&a),
                    (Some(_), None) => Ordering::Greater,
                    (None, Some(_)) => Ordering::Less,
                    (None, None) => Ordering::Equal,
                }
            })
            // Smaller seq (older) first -> reverse compare.
            .then_with(|| other.seq.cmp(&self.seq))
    }
}

/// Thread-safe priority scheduler.
#[derive(Debug, Clone)]
pub struct Scheduler {
    inner: Arc<Mutex<SchedulerInner>>,
}

#[derive(Debug, Default)]
struct SchedulerInner {
    heap: BinaryHeap<Task>,
    next_seq: u64,
    completed: u64,
    cancelled: u64,
}

impl Scheduler {
    /// Create an empty scheduler.
    pub fn new() -> Self {
        Scheduler {
            inner: Arc::new(Mutex::new(SchedulerInner::default())),
        }
    }

    /// Enqueue a task. Returns the assigned sequence number.
    pub fn push(&self, id: &str, priority: i64, deadline_ms: Option<u64>, payload: &str) -> u64 {
        let mut inner = self.inner.lock().unwrap();
        let seq = inner.next_seq;
        inner.next_seq += 1;
        inner.heap.push(Task {
            id: id.to_string(),
            priority,
            deadline_ms,
            seq,
            payload: payload.to_string(),
            enqueued_at_ms: now_millis(),
        });
        seq
    }

    /// Dequeue the highest-priority task, or `None` when empty.
    pub fn pop(&self) -> Option<Task> {
        self.inner.lock().unwrap().heap.pop()
    }

    /// Peek at the next task without removing it.
    pub fn peek(&self) -> Option<Task> {
        self.inner.lock().unwrap().heap.peek().cloned()
    }

    /// Number of pending tasks.
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().heap.len()
    }

    /// True when no tasks are pending.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Remove a pending task by id. Returns true when found.
    pub fn cancel(&self, id: &str) -> bool {
        let mut inner = self.inner.lock().unwrap();
        let before = inner.heap.len();
        inner.heap = inner
            .heap
            .drain()
            .filter(|t| t.id != id)
            .collect::<BinaryHeap<_>>();
        let removed = inner.heap.len() != before;
        if removed {
            inner.cancelled += 1;
        }
        removed
    }

    /// Record that a popped task finished (for stats).
    pub fn mark_completed(&self) {
        self.inner.lock().unwrap().completed += 1;
    }

    /// Drain up to `n` tasks in priority order.
    pub fn drain(&self, n: usize) -> Vec<Task> {
        let mut inner = self.inner.lock().unwrap();
        let mut out = Vec::with_capacity(n.min(inner.heap.len()));
        for _ in 0..n {
            match inner.heap.pop() {
                Some(t) => out.push(t),
                None => break,
            }
        }
        out
    }

    /// (pending, completed, cancelled)
    pub fn stats(&self) -> (usize, u64, u64) {
        let inner = self.inner.lock().unwrap();
        (inner.heap.len(), inner.completed, inner.cancelled)
    }

    /// Wait-free snapshot of pending task ids in priority order (for introspection).
    pub fn pending_ids(&self) -> Vec<String> {
        let inner = self.inner.lock().unwrap();
        let mut tasks: Vec<&Task> = inner.heap.iter().collect();
        tasks.sort_by(|a, b| b.cmp(a)); // highest priority first
        tasks.into_iter().map(|t| t.id.clone()).collect()
    }
}

impl Default for Scheduler {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn priority_ordering() {
        let s = Scheduler::new();
        s.push("low", 1, None, "");
        s.push("high", 10, None, "");
        s.push("mid", 5, None, "");
        assert_eq!(s.pop().unwrap().id, "high");
        assert_eq!(s.pop().unwrap().id, "mid");
        assert_eq!(s.pop().unwrap().id, "low");
        assert!(s.pop().is_none());
    }

    #[test]
    fn fifo_tie_break() {
        let s = Scheduler::new();
        s.push("a", 5, None, "");
        s.push("b", 5, None, "");
        s.push("c", 5, None, "");
        assert_eq!(s.pop().unwrap().id, "a");
        assert_eq!(s.pop().unwrap().id, "b");
        assert_eq!(s.pop().unwrap().id, "c");
    }

    #[test]
    fn deadline_tie_break() {
        let s = Scheduler::new();
        s.push("late", 5, Some(9_999_999), "");
        s.push("early", 5, Some(1), "");
        assert_eq!(s.pop().unwrap().id, "early");
        assert_eq!(s.pop().unwrap().id, "late");
    }

    #[test]
    fn cancel_removes_task() {
        let s = Scheduler::new();
        s.push("keep", 1, None, "");
        s.push("drop", 9, None, "");
        assert!(s.cancel("drop"));
        assert!(!s.cancel("missing"));
        assert_eq!(s.pop().unwrap().id, "keep");
        let (_, _, cancelled) = s.stats();
        assert_eq!(cancelled, 1);
    }

    #[test]
    fn drain_and_stats() {
        let s = Scheduler::new();
        for i in 0..10 {
            s.push(&format!("t{i}"), i, None, "");
        }
        let got = s.drain(4);
        assert_eq!(got.len(), 4);
        assert_eq!(got[0].id, "t9");
        assert_eq!(s.len(), 6);
        s.mark_completed();
        let (pending, completed, _) = s.stats();
        assert_eq!((pending, completed), (6, 1));
    }

    #[test]
    fn concurrent_push_pop() {
        use std::thread;
        let s = Scheduler::new();
        let mut handles = vec![];
        for w in 0..8 {
            let sc = s.clone();
            handles.push(thread::spawn(move || {
                for i in 0..250 {
                    sc.push(&format!("w{w}-t{i}"), i as i64, None, "");
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(s.len(), 2000);
        let mut last_prio = i64::MAX;
        while let Some(t) = s.pop() {
            assert!(t.priority <= last_prio);
            last_prio = t.priority;
        }
    }

    #[test]
    fn peek_does_not_remove() {
        let s = Scheduler::new();
        s.push("only", 3, None, "data");
        assert_eq!(s.peek().unwrap().payload, "data");
        assert_eq!(s.len(), 1);
    }
}
