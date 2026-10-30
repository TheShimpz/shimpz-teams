//! A bounded cache of reads from immutable Assistant containers, coordinated per container (`assistant/cache.py`).
//!
//! The lock only guards the map: a cold read never delays a warm hit for another container. Concurrent misses for one
//! container share a single read and its failure, while at most [`MAX_CONCURRENT_READS`] distinct reads run. A
//! container discarded while its read runs is not cached. Entries are evicted least recently used first, by count
//! and, when the cache has a weight budget, by weight (always keeping the newest entry).

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use tokio::sync::{OnceCell, Semaphore};

/// Distinct cold reads one cache runs at once; further misses wait without holding the map.
pub const MAX_CONCURRENT_READS: usize = 4;
/// Container generations an Assistant read cache keeps by default.
pub const DEFAULT_ENTRIES: usize = 256;

/// The container's Docker id when it is a bounded, safe cache key.
#[must_use]
pub fn container_id(identifier: &str) -> Option<&str> {
    let safe = (1..=256).contains(&identifier.len())
        && identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'));
    safe.then_some(identifier)
}

type Flight<T, E> = Arc<OnceCell<Result<T, E>>>;

struct State<T, E> {
    entries: VecDeque<(String, T, usize)>,
    flights: HashMap<String, Flight<T, E>>,
    weight: usize,
}

/// One bounded read cache keyed by container id.
pub struct ReadCache<T, E> {
    max_entries: usize,
    max_weight: Option<usize>,
    state: Mutex<State<T, E>>,
    reads: Semaphore,
}

impl<T, E> std::fmt::Debug for ReadCache<T, E> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReadCache")
            .field("max_entries", &self.max_entries)
            .finish_non_exhaustive()
    }
}

impl<T: Clone, E: Clone> ReadCache<T, E> {
    /// A cache of at most `max_entries` entries (at least one) and, if given, at most `max_weight` total weight.
    #[must_use]
    pub fn new(max_entries: usize, max_weight: Option<usize>) -> Self {
        Self {
            max_entries: max_entries.max(1),
            max_weight,
            state: Mutex::new(State {
                entries: VecDeque::new(),
                flights: HashMap::new(),
                weight: 0,
            }),
            reads: Semaphore::new(MAX_CONCURRENT_READS),
        }
    }

    fn locked(&self) -> MutexGuard<'_, State<T, E>> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The cached value of `key`, or the shared result of one `read`; `weight` sizes a value for the budget.
    ///
    /// # Errors
    /// The read's error, shared with every concurrent miss for the same key; an error is never cached.
    pub async fn get<F, Fut>(
        &self,
        key: &str,
        weight: impl Fn(&T) -> usize,
        read: F,
    ) -> Result<T, E>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Result<T, E>>,
    {
        let flight = {
            let mut state = self.locked();
            if let Some(index) = state.entries.iter().position(|(entry, _, _)| entry == key) {
                let entry = state
                    .entries
                    .remove(index)
                    .unwrap_or_else(|| unreachable!("the index is in bounds"));
                let value = entry.1.clone();
                state.entries.push_back(entry);
                return Ok(value);
            }
            Arc::clone(state.flights.entry(key.to_owned()).or_default())
        };
        let result = flight
            .get_or_init(|| async {
                let _permit = self.reads.acquire().await;
                read().await
            })
            .await
            .clone();
        self.land(key, &flight, &weight);
        result
    }

    fn land(&self, key: &str, flight: &Flight<T, E>, weight: &impl Fn(&T) -> usize) {
        let mut state = self.locked();
        if !state
            .flights
            .get(key)
            .is_some_and(|current| Arc::ptr_eq(current, flight))
        {
            return;
        }
        state.flights.remove(key);
        if let Some(Ok(value)) = flight.get() {
            let size = weight(value);
            state.weight += size;
            state
                .entries
                .push_back((key.to_owned(), value.clone(), size));
            while state.entries.len() > self.max_entries
                || (self.max_weight.is_some_and(|budget| state.weight > budget)
                    && state.entries.len() > 1)
            {
                if let Some((_, _, evicted)) = state.entries.pop_front() {
                    state.weight -= evicted;
                }
            }
        }
    }

    /// Forgets one container's entry and any read in flight.
    pub fn discard(&self, key: &str) {
        let mut state = self.locked();
        state.flights.remove(key);
        if let Some(index) = state.entries.iter().position(|(entry, _, _)| entry == key)
            && let Some((_, _, size)) = state.entries.remove(index)
        {
            state.weight -= size;
        }
    }

    /// How many entries are cached.
    #[must_use]
    pub fn len(&self) -> usize {
        self.locked().entries.len()
    }

    /// Whether nothing is cached.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::{ReadCache, container_id};

    #[tokio::test]
    async fn concurrent_misses_share_one_read_and_a_failure_is_not_cached() {
        let cache: Arc<ReadCache<u32, &'static str>> = Arc::new(ReadCache::new(2, None));
        let reads = Arc::new(AtomicUsize::new(0));
        let read = |value: Result<u32, &'static str>| {
            let reads = Arc::clone(&reads);
            move || async move {
                reads.fetch_add(1, Ordering::SeqCst);
                tokio::task::yield_now().await;
                value
            }
        };
        let (left, right) = tokio::join!(
            cache.get("a", |_| 1, read(Ok(7))),
            cache.get("a", |_| 1, read(Ok(8)))
        );
        assert_eq!((left, right), (Ok(7), Ok(7)));
        assert_eq!(reads.load(Ordering::SeqCst), 1);
        assert_eq!(
            cache.get("b", |_| 1, read(Err("unavailable"))).await,
            Err("unavailable")
        );
        assert_eq!(cache.get("b", |_| 1, read(Ok(9))).await, Ok(9));
        assert_eq!(reads.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn eviction_is_least_recently_used_by_count_and_weight() {
        let cache: ReadCache<u32, ()> = ReadCache::new(2, Some(10));
        for key in ["a", "b"] {
            cache
                .get(key, |_| 4, || async { Ok(1) })
                .await
                .expect("read");
        }
        cache
            .get("a", |_| 4, || async { Ok(2) })
            .await
            .expect("hit");
        cache
            .get("c", |_| 4, || async { Ok(3) })
            .await
            .expect("read");
        assert_eq!(cache.get("a", |_| 4, || async { Ok(9) }).await, Ok(1));
        assert_eq!(cache.get("b", |_| 4, || async { Ok(9) }).await, Ok(9));
        cache
            .get("d", |_| 20, || async { Ok(4) })
            .await
            .expect("read");
        assert_eq!(cache.len(), 1);
        cache.discard("d");
        assert!(cache.is_empty());
    }

    #[test]
    fn only_bounded_safe_ids_are_keys() {
        assert!(container_id("0123abcd_-.").is_some());
        for id in ["", "a/b", "a b", "é", &"a".repeat(257)] {
            assert!(container_id(id).is_none(), "{id:?}");
        }
    }
}
