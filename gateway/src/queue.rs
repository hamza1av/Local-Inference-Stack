//! Per-backend concurrency limit with a two-class priority queue.
//!
//! `interactive` waiters are always served before `background` ones, FIFO within a class.
//! A waiter whose future is dropped (client disconnected) leaves the queue immediately;
//! if it had just been granted a permit, the permit is passed on to the next waiter.

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};

use tokio::sync::oneshot;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Priority {
    Interactive = 0,
    Background = 1,
}

impl Priority {
    pub fn from_header(v: Option<&str>) -> Priority {
        match v.map(|s| s.trim().to_ascii_lowercase()) {
            Some(s) if s == "background" || s == "batch" || s == "low" => Priority::Background,
            _ => Priority::Interactive,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Priority::Interactive => "interactive",
            Priority::Background => "background",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum QueueError {
    /// Queue already holds `max_queue` waiters.
    Full,
    /// Waited longer than the queue timeout.
    Timeout,
}

struct Waiter {
    id: u64,
    tx: oneshot::Sender<()>,
}

struct State {
    in_use: usize,
    next_id: u64,
    queues: [VecDeque<Waiter>; 2],
}

struct Inner {
    max: usize,
    max_queue: usize,
    state: Mutex<State>,
}

#[derive(Clone)]
pub struct Limiter(Arc<Inner>);

/// Held while a request runs against the backend; dropping it frees the slot.
pub struct Permit(Arc<Inner>);

impl Drop for Permit {
    fn drop(&mut self) {
        release(&self.0);
    }
}

/// Hand the slot to the next live waiter, or free it.
fn release(inner: &Inner) {
    let mut st = inner.state.lock().unwrap();
    loop {
        let next = st.queues[0].pop_front().or_else(|| st.queues[1].pop_front());
        match next {
            // Slot ownership moves to the waiter (in_use unchanged).
            Some(w) => {
                if w.tx.send(()).is_ok() {
                    return;
                }
            }
            None => {
                st.in_use = st.in_use.saturating_sub(1);
                return;
            }
        }
    }
}

/// Removes a waiter from the queue when its future is dropped before completion.
struct WaitGuard<'a> {
    inner: &'a Arc<Inner>,
    id: u64,
    prio: usize,
    done: bool,
}

impl Drop for WaitGuard<'_> {
    fn drop(&mut self) {
        if self.done {
            return;
        }
        let mut st = self.inner.state.lock().unwrap();
        let q = &mut st.queues[self.prio];
        if let Some(pos) = q.iter().position(|w| w.id == self.id) {
            q.remove(pos);
        } else {
            // Not queued any more: the permit was handed to us but nobody will use it.
            drop(st);
            release(self.inner);
        }
    }
}

impl Limiter {
    pub fn new(max: usize, max_queue: usize) -> Limiter {
        Limiter(Arc::new(Inner {
            max: max.max(1),
            max_queue,
            state: Mutex::new(State {
                in_use: 0,
                next_id: 0,
                queues: [VecDeque::new(), VecDeque::new()],
            }),
        }))
    }

    /// (in flight, queued)
    pub fn stats(&self) -> (usize, usize) {
        let st = self.0.state.lock().unwrap();
        (st.in_use, st.queues[0].len() + st.queues[1].len())
    }

    pub async fn acquire(&self, prio: Priority, timeout: Duration) -> Result<Permit, QueueError> {
        let (id, rx) = {
            let mut st = self.0.state.lock().unwrap();
            let queued = st.queues[0].len() + st.queues[1].len();
            if st.in_use < self.0.max && queued == 0 {
                st.in_use += 1;
                return Ok(Permit(self.0.clone()));
            }
            if queued >= self.0.max_queue {
                return Err(QueueError::Full);
            }
            let (tx, rx) = oneshot::channel();
            let id = st.next_id;
            st.next_id += 1;
            st.queues[prio as usize].push_back(Waiter { id, tx });
            (id, rx)
        };
        let mut guard = WaitGuard { inner: &self.0, id, prio: prio as usize, done: false };
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(())) => {
                guard.done = true;
                Ok(Permit(self.0.clone()))
            }
            // Sender dropped without sending cannot happen (release always sends or the
            // waiter is removed); treat like a timeout. The guard cleans up either way.
            Ok(Err(_)) | Err(_) => Err(QueueError::Timeout),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    const LONG: Duration = Duration::from_secs(10);

    #[tokio::test]
    async fn immediate_when_free() {
        let l = Limiter::new(2, 4);
        let a = l.acquire(Priority::Interactive, LONG).await.unwrap();
        let _b = l.acquire(Priority::Background, LONG).await.unwrap();
        assert_eq!(l.stats(), (2, 0));
        drop(a);
        assert_eq!(l.stats(), (1, 0));
    }

    #[tokio::test]
    async fn interactive_before_background_fifo_within_class() {
        let l = Limiter::new(1, 16);
        let first = l.acquire(Priority::Interactive, LONG).await.unwrap();
        let order = Arc::new(StdMutex::new(Vec::new()));
        let mut tasks = Vec::new();
        for (name, prio) in [
            ("b1", Priority::Background),
            ("i1", Priority::Interactive),
            ("b2", Priority::Background),
            ("i2", Priority::Interactive),
        ] {
            let (lc, order) = (l.clone(), order.clone());
            tasks.push(tokio::spawn(async move {
                let _p = lc.acquire(prio, LONG).await.unwrap();
                order.lock().unwrap().push(name);
                tokio::time::sleep(Duration::from_millis(5)).await;
            }));
            // make enqueue order deterministic
            while l.stats().1 < tasks.len() {
                tokio::task::yield_now().await;
            }
        }
        drop(first);
        for t in tasks {
            t.await.unwrap();
        }
        assert_eq!(*order.lock().unwrap(), vec!["i1", "i2", "b1", "b2"]);
        assert_eq!(l.stats(), (0, 0));
    }

    #[tokio::test]
    async fn full_queue_rejects() {
        let l = Limiter::new(1, 1);
        let _p = l.acquire(Priority::Interactive, LONG).await.unwrap();
        let l2 = l.clone();
        let waiting = tokio::spawn(async move { l2.acquire(Priority::Interactive, LONG).await.map(|_| ()) });
        while l.stats().1 < 1 {
            tokio::task::yield_now().await;
        }
        assert_eq!(l.acquire(Priority::Interactive, LONG).await.err(), Some(QueueError::Full));
        waiting.abort();
    }

    #[tokio::test]
    async fn timeout_leaves_queue() {
        let l = Limiter::new(1, 4);
        let _p = l.acquire(Priority::Interactive, LONG).await.unwrap();
        let r = l.acquire(Priority::Background, Duration::from_millis(20)).await;
        assert_eq!(r.err(), Some(QueueError::Timeout));
        assert_eq!(l.stats(), (1, 0));
    }

    #[tokio::test]
    async fn cancelled_waiter_leaves_queue_and_slot_is_reused() {
        let l = Limiter::new(1, 4);
        let p = l.acquire(Priority::Interactive, LONG).await.unwrap();
        let l2 = l.clone();
        let t = tokio::spawn(async move { l2.acquire(Priority::Interactive, LONG).await.map(|_| ()) });
        while l.stats().1 < 1 {
            tokio::task::yield_now().await;
        }
        t.abort();
        let _ = t.await;
        assert_eq!(l.stats(), (1, 0));
        drop(p);
        assert_eq!(l.stats(), (0, 0));
        let _again = l.acquire(Priority::Interactive, Duration::from_millis(50)).await.unwrap();
    }

    #[tokio::test]
    async fn granted_but_dropped_permit_is_passed_on() {
        let l = Limiter::new(1, 4);
        let p = l.acquire(Priority::Interactive, LONG).await.unwrap();
        // Build the waiting future but drop it after the grant, before it is polled again.
        let mut fut = Box::pin(l.acquire(Priority::Interactive, LONG));
        assert!(futures_util::poll!(&mut fut).is_pending());
        drop(p); // grants to the pending future
        assert_eq!(l.stats(), (1, 0));
        drop(fut); // never observed the grant -> slot must be freed
        assert_eq!(l.stats(), (0, 0));
    }

    #[test]
    fn priority_header() {
        assert_eq!(Priority::from_header(None), Priority::Interactive);
        assert_eq!(Priority::from_header(Some("Background")), Priority::Background);
        assert_eq!(Priority::from_header(Some("interactive")), Priority::Interactive);
        assert_eq!(Priority::from_header(Some("weird")), Priority::Interactive);
    }
}
