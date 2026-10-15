//! High-throughput multi-producer multi-consumer message queue.
//!
//! Backed by `crossbeam-channel` (bounded or unbounded). Used for
//! internal service communication and worker dispatch.

use crossbeam_channel::{bounded, unbounded, Receiver, Sender, TryRecvError};
use std::time::Duration;

/// A typed message envelope with metadata useful for tracing.
#[derive(Debug, Clone)]
pub struct Message {
    /// Message kind / topic.
    pub kind: String,
    /// Serialized body (JSON string).
    pub body: String,
    /// Epoch millis when sent.
    pub sent_at_ms: u64,
    /// Sender identifier for tracing.
    pub sender: String,
}

fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Thread-safe MPMC queue.
#[derive(Debug, Clone)]
pub struct MessageQueue {
    tx: Sender<Message>,
    rx: Receiver<Message>,
    capacity: Option<usize>,
}

impl MessageQueue {
    /// Create an unbounded queue (use with care; prefer bounded in production).
    pub fn unbounded() -> Self {
        let (tx, rx) = unbounded();
        MessageQueue {
            tx,
            rx,
            capacity: None,
        }
    }

    /// Create a bounded queue with the given capacity.
    pub fn bounded(capacity: usize) -> Self {
        let (tx, rx) = bounded(capacity);
        MessageQueue {
            tx,
            rx,
            capacity: Some(capacity),
        }
    }

    /// Blocking send.
    pub fn send(&self, kind: &str, body: &str, sender: &str) -> bool {
        self.tx
            .send(Message {
                kind: kind.to_string(),
                body: body.to_string(),
                sent_at_ms: now_millis(),
                sender: sender.to_string(),
            })
            .is_ok()
    }

    /// Non-blocking send; false when the queue is full (bounded) or disconnected.
    pub fn try_send(&self, kind: &str, body: &str, sender: &str) -> bool {
        matches!(
            self.tx.try_send(Message {
                kind: kind.to_string(),
                body: body.to_string(),
                sent_at_ms: now_millis(),
                sender: sender.to_string(),
            }),
            Ok(())
        )
    }

    /// Non-blocking receive. Returns None when empty.
    pub fn try_recv(&self) -> Option<Message> {
        match self.rx.try_recv() {
            Ok(m) => Some(m),
            Err(TryRecvError::Empty) | Err(TryRecvError::Disconnected) => None,
        }
    }

    /// Blocking receive with timeout in millis. None on timeout.
    pub fn recv_timeout(&self, timeout_ms: u64) -> Option<Message> {
        self.rx
            .recv_timeout(Duration::from_millis(timeout_ms))
            .ok()
    }

    /// Drain everything currently queued.
    pub fn drain(&self) -> Vec<Message> {
        let mut out = Vec::new();
        while let Ok(m) = self.rx.try_recv() {
            out.push(m);
        }
        out
    }

    /// Approximate number of queued messages.
    pub fn len(&self) -> usize {
        self.rx.len()
    }

    /// True when no messages are queued.
    pub fn is_empty(&self) -> bool {
        self.rx.is_empty()
    }

    /// Configured capacity (None = unbounded).
    pub fn capacity(&self) -> Option<usize> {
        self.capacity
    }
}

/// Fan-out: one queue feeding N worker queues (simple broadcast cloner).
pub struct Broadcaster {
    subs: Vec<Sender<Message>>,
}

impl Broadcaster {
    pub fn new() -> Self {
        Broadcaster { subs: Vec::new() }
    }

    /// Subscribe a new listener; returns its receiving queue handle.
    pub fn subscribe(&mut self, capacity: usize) -> MessageQueue {
        let (tx, rx) = bounded(capacity);
        self.subs.push(tx);
        MessageQueue {
            tx: self.subs.last().unwrap().clone(),
            rx,
            capacity: Some(capacity),
        }
    }

    /// Publish to all subscribers. Returns number of successful deliveries.
    pub fn publish(&self, kind: &str, body: &str, sender: &str) -> usize {
        let msg = Message {
            kind: kind.to_string(),
            body: body.to_string(),
            sent_at_ms: now_millis(),
            sender: sender.to_string(),
        };
        self.subs
            .iter()
            .filter(|tx| tx.try_send(msg.clone()).is_ok())
            .count()
    }

    pub fn subscriber_count(&self) -> usize {
        self.subs.len()
    }
}

impl Default for Broadcaster {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn send_recv_roundtrip() {
        let q = MessageQueue::bounded(16);
        assert!(q.send("task", "{\"a\":1}", "t1"));
        let m = q.try_recv().unwrap();
        assert_eq!(m.kind, "task");
        assert_eq!(m.body, "{\"a\":1}");
        assert_eq!(m.sender, "t1");
        assert!(q.is_empty());
    }

    #[test]
    fn bounded_backpressure() {
        let q = MessageQueue::bounded(2);
        assert!(q.try_send("k", "1", "s"));
        assert!(q.try_send("k", "2", "s"));
        assert!(!q.try_send("k", "3", "s")); // full
        assert_eq!(q.len(), 2);
    }

    #[test]
    fn mpmc_throughput() {
        let q = MessageQueue::unbounded();
        let n_producers = 4;
        let per = 1000;
        let mut handles = vec![];
        for p in 0..n_producers {
            let qc = q.clone();
            handles.push(thread::spawn(move || {
                for i in 0..per {
                    qc.send("m", &i.to_string(), &format!("p{p}"));
                }
            }));
        }
        let qc = q.clone();
        let consumer = thread::spawn(move || {
            let mut got = 0;
            while got < n_producers * per {
                if qc.recv_timeout(5000).is_some() {
                    got += 1;
                }
            }
            got
        });
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(consumer.join().unwrap(), n_producers * per);
    }

    #[test]
    fn broadcaster_fanout() {
        let mut b = Broadcaster::new();
        let q1 = b.subscribe(8);
        let q2 = b.subscribe(8);
        assert_eq!(b.subscriber_count(), 2);
        assert_eq!(b.publish("evt", "{}", "pub"), 2);
        assert_eq!(q1.try_recv().unwrap().kind, "evt");
        assert_eq!(q2.try_recv().unwrap().kind, "evt");
    }

    #[test]
    fn recv_timeout_empty() {
        let q = MessageQueue::bounded(4);
        assert!(q.recv_timeout(20).is_none());
    }
}
