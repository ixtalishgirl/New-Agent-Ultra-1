//! Haley Rust core — PyO3 module `haley_rust_core`.
//!
//! High-performance primitives for the Haley autonomous coding agent:
//! priority scheduler, MPMC message queues, resource monitoring,
//! structured event log, rate limiting, fast JSON helpers, tool-execution
//! metadata, worker pool, log processing, and file indexing.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

pub mod events;
pub mod index;
pub mod logproc;
pub mod monitor;
pub mod queue;
pub mod ratelimit;
pub mod scheduler;
pub mod serde_fast;
pub mod worker;

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

#[pyclass]
struct PyScheduler {
    inner: scheduler::Scheduler,
}

#[pymethods]
impl PyScheduler {
    #[new]
    fn new() -> Self {
        PyScheduler {
            inner: scheduler::Scheduler::new(),
        }
    }

    #[pyo3(signature = (id, priority, deadline_ms=None, payload=""))]
    fn push(
        &self,
        id: &str,
        priority: i64,
        deadline_ms: Option<u64>,
        payload: &str,
    ) -> PyResult<u64> {
        Ok(self.inner.push(id, priority, deadline_ms, payload))
    }

    fn pop(&self, py: Python<'_>) -> PyResult<PyObject> {
        match self.inner.pop() {
            Some(t) => task_to_dict(py, &t),
            None => Ok(py.None()),
        }
    }

    fn peek(&self, py: Python<'_>) -> PyResult<PyObject> {
        match self.inner.peek() {
            Some(t) => task_to_dict(py, &t),
            None => Ok(py.None()),
        }
    }

    fn cancel(&self, id: &str) -> bool {
        self.inner.cancel(id)
    }

    fn mark_completed(&self) {
        self.inner.mark_completed();
    }

    fn drain(&self, py: Python<'_>, n: usize) -> PyResult<PyObject> {
        let tasks = self.inner.drain(n);
        let list = PyList::empty_bound(py);
        for t in &tasks {
            list.append(task_to_dict(py, t)?)?;
        }
        Ok(list.into())
    }

    fn len(&self) -> usize {
        self.inner.len()
    }

    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    fn stats(&self) -> (usize, u64, u64) {
        self.inner.stats()
    }

    fn pending_ids(&self) -> Vec<String> {
        self.inner.pending_ids()
    }
}

fn task_to_dict(py: Python<'_>, t: &scheduler::Task) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    d.set_item("id", &t.id)?;
    d.set_item("priority", t.priority)?;
    d.set_item("deadline_ms", t.deadline_ms)?;
    d.set_item("seq", t.seq)?;
    d.set_item("payload", &t.payload)?;
    d.set_item("enqueued_at_ms", t.enqueued_at_ms)?;
    Ok(d.into())
}

// ---------------------------------------------------------------------------
// MessageQueue
// ---------------------------------------------------------------------------

#[pyclass]
struct PyMessageQueue {
    inner: queue::MessageQueue,
}

#[pymethods]
impl PyMessageQueue {
    #[staticmethod]
    fn unbounded() -> Self {
        PyMessageQueue {
            inner: queue::MessageQueue::unbounded(),
        }
    }

    #[staticmethod]
    fn bounded(capacity: usize) -> Self {
        PyMessageQueue {
            inner: queue::MessageQueue::bounded(capacity),
        }
    }

    fn send(&self, kind: &str, body: &str, sender: &str) -> bool {
        self.inner.send(kind, body, sender)
    }

    fn try_send(&self, kind: &str, body: &str, sender: &str) -> bool {
        self.inner.try_send(kind, body, sender)
    }

    fn try_recv(&self, py: Python<'_>) -> PyResult<PyObject> {
        match self.inner.try_recv() {
            Some(m) => message_to_dict(py, &m),
            None => Ok(py.None()),
        }
    }

    fn recv_timeout(&self, py: Python<'_>, timeout_ms: u64) -> PyResult<PyObject> {
        // Release the GIL while blocking.
        let msg = py.allow_threads(|| self.inner.recv_timeout(timeout_ms));
        match msg {
            Some(m) => message_to_dict(py, &m),
            None => Ok(py.None()),
        }
    }

    fn drain(&self, py: Python<'_>) -> PyResult<PyObject> {
        let msgs = self.inner.drain();
        let list = PyList::empty_bound(py);
        for m in &msgs {
            list.append(message_to_dict(py, m)?)?;
        }
        Ok(list.into())
    }

    fn len(&self) -> usize {
        self.inner.len()
    }

    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }
}

fn message_to_dict(py: Python<'_>, m: &queue::Message) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    d.set_item("kind", &m.kind)?;
    d.set_item("body", &m.body)?;
    d.set_item("sent_at_ms", m.sent_at_ms)?;
    d.set_item("sender", &m.sender)?;
    Ok(d.into())
}

// ---------------------------------------------------------------------------
// ResourceMonitor
// ---------------------------------------------------------------------------

#[pyclass]
struct PyResourceMonitor {
    inner: monitor::ResourceMonitor,
}

#[pymethods]
impl PyResourceMonitor {
    #[new]
    fn new() -> Self {
        PyResourceMonitor {
            inner: monitor::ResourceMonitor::new(),
        }
    }

    fn sample(&mut self, py: Python<'_>) -> PyResult<PyObject> {
        let s = py.allow_threads(|| self.inner.sample());
        let d = PyDict::new_bound(py);
        d.set_item("cpu_percent", s.cpu_percent)?;
        d.set_item("mem_total_kb", s.mem.total_kb)?;
        d.set_item("mem_available_kb", s.mem.available_kb)?;
        d.set_item("mem_used_kb", s.mem.used_kb)?;
        d.set_item("mem_used_percent", s.mem.used_percent)?;
        d.set_item("self_rss_kb", s.self_rss_kb)?;
        d.set_item("cpu_count", s.cpu_count)?;
        Ok(d.into())
    }

    fn memory_pressure(&mut self, threshold_percent: f64) -> bool {
        self.inner.memory_pressure(threshold_percent)
    }
}

#[pyfunction]
fn quick_cpu_count() -> usize {
    monitor::quick_cpu_count()
}

#[pyfunction]
fn quick_mem_used_percent() -> f64 {
    monitor::quick_mem_info().used_percent
}

#[pyfunction]
fn quick_self_rss_kb() -> u64 {
    monitor::quick_self_rss_kb()
}

// ---------------------------------------------------------------------------
// EventLog
// ---------------------------------------------------------------------------

#[pyclass]
struct PyEventLog {
    inner: events::EventLog,
}

#[pymethods]
impl PyEventLog {
    #[new]
    fn new(capacity: usize) -> Self {
        PyEventLog {
            inner: events::EventLog::new(capacity),
        }
    }

    #[pyo3(signature = (kind, source, level, message, fields_json="{}"))]
    fn emit(
        &self,
        kind: &str,
        source: &str,
        level: &str,
        message: &str,
        fields_json: &str,
    ) -> u64 {
        self.inner.emit(kind, source, level, message, fields_json)
    }

    #[pyo3(signature = (kind_prefix="", min_level="debug", limit=0))]
    fn query(
        &self,
        py: Python<'_>,
        kind_prefix: &str,
        min_level: &str,
        limit: usize,
    ) -> PyResult<PyObject> {
        let evts = self.inner.query(kind_prefix, min_level, limit);
        let list = PyList::empty_bound(py);
        for e in &evts {
            let d = PyDict::new_bound(py);
            d.set_item("kind", &e.kind)?;
            d.set_item("source", &e.source)?;
            d.set_item("level", &e.level)?;
            d.set_item("message", &e.message)?;
            d.set_item("fields_json", &e.fields_json)?;
            d.set_item("ts_nanos", e.ts_nanos)?;
            d.set_item("seq", e.seq)?;
            list.append(d)?;
        }
        Ok(list.into())
    }

    fn count(&self, kind_prefix: &str) -> usize {
        self.inner.count(kind_prefix)
    }

    fn len(&self) -> usize {
        self.inner.len()
    }

    fn dropped(&self) -> u64 {
        self.inner.dropped()
    }

    fn clear(&self) {
        self.inner.clear();
    }
}

// ---------------------------------------------------------------------------
// RateLimiter / SlidingWindow
// ---------------------------------------------------------------------------

#[pyclass]
struct PyRateLimiter {
    inner: ratelimit::RateLimiter,
}

#[pymethods]
impl PyRateLimiter {
    #[new]
    fn new(capacity: f64, refill_per_sec: f64) -> Self {
        PyRateLimiter {
            inner: ratelimit::RateLimiter::new(capacity, refill_per_sec),
        }
    }

    fn check(&self, key: &str) -> (bool, f64) {
        self.inner.check(key)
    }

    fn check_n(&self, key: &str, n: f64) -> (bool, f64) {
        self.inner.check_n(key, n)
    }

    fn reset(&self, key: &str) {
        self.inner.reset(key);
    }

    fn stats(&self) -> (u64, u64, usize) {
        self.inner.stats()
    }
}

#[pyclass]
struct PySlidingWindow {
    inner: ratelimit::SlidingWindow,
}

#[pymethods]
impl PySlidingWindow {
    #[new]
    fn new(window_secs: u64) -> Self {
        PySlidingWindow {
            inner: ratelimit::SlidingWindow::new(window_secs),
        }
    }

    fn record(&self, key: &str) -> usize {
        self.inner.record(key)
    }

    fn count(&self, key: &str) -> usize {
        self.inner.count(key)
    }
}

// ---------------------------------------------------------------------------
// ToolMeta + JSON helpers
// ---------------------------------------------------------------------------

#[pyclass]
struct PyToolMeta {
    inner: serde_fast::ToolMeta,
}

#[pymethods]
impl PyToolMeta {
    #[new]
    fn new(capacity: usize) -> Self {
        PyToolMeta {
            inner: serde_fast::ToolMeta::new(capacity),
        }
    }

    #[pyo3(signature = (tool, status, duration_ms, error=""))]
    fn record(&self, tool: &str, status: &str, duration_ms: u64, error: &str) {
        self.inner.record(tool, status, duration_ms, error);
    }

    fn tool_stats(&self, tool: &str) -> (u64, u64, f64) {
        self.inner.tool_stats(tool)
    }

    fn slowest(&self, py: Python<'_>, n: usize) -> PyResult<PyObject> {
        let v = self.inner.slowest(n);
        let list = PyList::empty_bound(py);
        for (name, avg, calls) in v {
            let obj: PyObject = (name, avg, calls).into_py(py);
            list.append(obj)?;
        }
        Ok(list.into())
    }

    fn len(&self) -> usize {
        self.inner.len()
    }
}

#[pyfunction]
fn json_validate(s: &str) -> (bool, String) {
    serde_fast::validate_json(s)
}

#[pyfunction]
fn json_get_field(json: &str, field: &str, py: Python<'_>) -> PyResult<PyObject> {
    match serde_fast::get_field(json, field) {
        Some(v) => Ok(v.into_py(py)),
        None => Ok(py.None()),
    }
}

#[pyfunction]
fn json_merge(a: &str, b: &str) -> String {
    serde_fast::merge_objects(a, b)
}

#[pyfunction]
fn json_encode_str_list(items: Vec<String>) -> String {
    let refs: Vec<&str> = items.iter().map(|s| s.as_str()).collect();
    serde_fast::encode_str_list(&refs)
}

#[pyfunction]
fn json_decode_str_list(s: &str) -> Vec<String> {
    serde_fast::decode_str_list(s)
}

// ---------------------------------------------------------------------------
// WorkerPool
// ---------------------------------------------------------------------------

#[pyclass]
struct PyWorkerPool {
    inner: Option<worker::WorkerPool>,
}

#[pymethods]
impl PyWorkerPool {
    #[new]
    fn new(workers: usize, queue_capacity: usize, handler: PyObject) -> PyResult<Self> {
        // The handler runs on Rust worker threads; call back into Python.
        // GIL is acquired per job — suitable for I/O-bound or bursty CPU work.
        let pool = worker::WorkerPool::new(workers, queue_capacity, move |job: &worker::Job| {
            Python::with_gil(|py| {
                let payload = job.payload.clone();
                match handler.call1(py, (job.id.clone(), job.kind.clone(), payload)) {
                    Ok(res) => {
                        let parsed: Result<(bool, String), _> = res.extract(py);
                        parsed.unwrap_or_else(|_| {
                            (false, "handler returned non (bool, str) tuple".to_string())
                        })
                    }
                    Err(e) => (false, format!("handler raised: {e}")),
                }
            })
        });
        Ok(PyWorkerPool { inner: Some(pool) })
    }

    fn submit(&self, id: &str, kind: &str, payload: &str) -> PyResult<bool> {
        match &self.inner {
            Some(p) => Ok(p.submit(id, kind, payload)),
            None => Err(PyValueError::new_err("worker pool is shut down")),
        }
    }

    fn poll_result(&self, py: Python<'_>) -> PyResult<PyObject> {
        match &self.inner {
            Some(p) => match p.poll_result() {
                Some((job_id, ok, output)) => Ok((job_id, ok, output).into_py(py)),
                None => Ok(py.None()),
            },
            None => Err(PyValueError::new_err("worker pool is shut down")),
        }
    }

    fn wait_for(&self, py: Python<'_>, count: usize, timeout_ms: u64) -> PyResult<PyObject> {
        let results = match &self.inner {
            Some(p) => py.allow_threads(|| p.wait_for(count, timeout_ms)),
            None => return Err(PyValueError::new_err("worker pool is shut down")),
        };
        let list = PyList::empty_bound(py);
        for (job_id, ok, output) in results {
            let obj: PyObject = (job_id, ok, output).into_py(py);
            list.append(obj)?;
        }
        Ok(list.into())
    }

    fn stats(&self) -> PyResult<(u64, u64, u64, usize)> {
        match &self.inner {
            Some(p) => Ok(p.stats()),
            None => Err(PyValueError::new_err("worker pool is shut down")),
        }
    }

    fn worker_count(&self) -> PyResult<usize> {
        match &self.inner {
            Some(p) => Ok(p.worker_count()),
            None => Err(PyValueError::new_err("worker pool is shut down")),
        }
    }

    fn shutdown(mut slf: PyRefMut<'_, Self>) -> PyResult<()> {
        if let Some(pool) = slf.inner.take() {
            // Release GIL while joining threads.
            slf.py().allow_threads(|| pool.shutdown());
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Log processing
// ---------------------------------------------------------------------------

#[pyfunction]
fn log_count_lines(path: &str) -> PyResult<u64> {
    logproc::count_lines(std::path::Path::new(path))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn log_grep_count(path: &str, needle: &str) -> PyResult<u64> {
    logproc::grep_count(std::path::Path::new(path), needle.as_bytes())
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn log_tail_lines(path: &str, n: usize) -> PyResult<Vec<String>> {
    logproc::tail_lines(std::path::Path::new(path), n)
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn log_find_offsets(path: &str, needle: &str, limit: usize) -> PyResult<Vec<u64>> {
    logproc::find_offsets(std::path::Path::new(path), needle.as_bytes(), limit)
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

// ---------------------------------------------------------------------------
// File indexing
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (root, max_depth=None, skip_hidden=None, include_exts=None))]
fn index_directory(
    py: Python<'_>,
    root: &str,
    max_depth: Option<usize>,
    skip_hidden: Option<bool>,
    include_exts: Option<Vec<String>>,
) -> PyResult<PyObject> {
    let mut opts = index::IndexOptions::default();
    if let Some(d) = max_depth {
        opts.max_depth = d;
    }
    if let Some(s) = skip_hidden {
        opts.skip_hidden = s;
    }
    if let Some(e) = include_exts {
        opts.include_exts = e;
    }
    let entries = py
        .allow_threads(|| index::index_dir(std::path::Path::new(root), &opts))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let list = PyList::empty_bound(py);
    for en in &entries {
        let d = PyDict::new_bound(py);
        d.set_item("path", &en.path)?;
        d.set_item("size_bytes", en.size_bytes)?;
        d.set_item("modified_ms", en.modified_ms)?;
        d.set_item("is_dir", en.is_dir)?;
        d.set_item("ext", &en.ext)?;
        list.append(d)?;
    }
    Ok(list.into())
}

#[pyfunction]
#[pyo3(signature = (entries, query, limit=50))]
fn index_search(
    py: Python<'_>,
    entries: &Bound<'_, PyList>,
    query: &str,
    limit: usize,
) -> PyResult<PyObject> {
    let mut owned = Vec::with_capacity(entries.len());
    for item in entries.iter() {
        let d: Bound<'_, PyDict> = item.downcast_into::<PyDict>()?;
        owned.push(index::FileEntry {
            path: d.get_item("path")?.and_then(|v| v.extract().ok()).unwrap_or_default(),
            size_bytes: d.get_item("size_bytes")?.and_then(|v| v.extract().ok()).unwrap_or(0),
            modified_ms: d.get_item("modified_ms")?.and_then(|v| v.extract().ok()).unwrap_or(0),
            is_dir: d.get_item("is_dir")?.and_then(|v| v.extract().ok()).unwrap_or(false),
            ext: d.get_item("ext")?.and_then(|v| v.extract().ok()).unwrap_or_default(),
        });
    }
    let hits = index::search(&owned, query, limit);
    let out = PyList::empty_bound(py);
    for h in hits {
        let d = PyDict::new_bound(py);
        d.set_item("path", &h.path)?;
        d.set_item("size_bytes", h.size_bytes)?;
        d.set_item("modified_ms", h.modified_ms)?;
        d.set_item("is_dir", h.is_dir)?;
        d.set_item("ext", &h.ext)?;
        out.append(d)?;
    }
    Ok(out.into())
}

// ---------------------------------------------------------------------------
// Module
// ---------------------------------------------------------------------------

#[pymodule]
fn haley_rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyScheduler>()?;
    m.add_class::<PyMessageQueue>()?;
    m.add_class::<PyResourceMonitor>()?;
    m.add_class::<PyEventLog>()?;
    m.add_class::<PyRateLimiter>()?;
    m.add_class::<PySlidingWindow>()?;
    m.add_class::<PyToolMeta>()?;
    m.add_class::<PyWorkerPool>()?;
    m.add_function(wrap_pyfunction!(quick_cpu_count, m)?)?;
    m.add_function(wrap_pyfunction!(quick_mem_used_percent, m)?)?;
    m.add_function(wrap_pyfunction!(quick_self_rss_kb, m)?)?;
    m.add_function(wrap_pyfunction!(json_validate, m)?)?;
    m.add_function(wrap_pyfunction!(json_get_field, m)?)?;
    m.add_function(wrap_pyfunction!(json_merge, m)?)?;
    m.add_function(wrap_pyfunction!(json_encode_str_list, m)?)?;
    m.add_function(wrap_pyfunction!(json_decode_str_list, m)?)?;
    m.add_function(wrap_pyfunction!(log_count_lines, m)?)?;
    m.add_function(wrap_pyfunction!(log_grep_count, m)?)?;
    m.add_function(wrap_pyfunction!(log_tail_lines, m)?)?;
    m.add_function(wrap_pyfunction!(log_find_offsets, m)?)?;
    m.add_function(wrap_pyfunction!(index_directory, m)?)?;
    m.add_function(wrap_pyfunction!(index_search, m)?)?;
    m.add("__version__", "1.0.0")?;
    Ok(())
}
