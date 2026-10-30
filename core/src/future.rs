//! The boxed future every object-safe Team seam returns.

use std::future::Future;
use std::pin::Pin;

/// A sendable future borrowed for `'a`, so a seam can be held as `Arc<dyn Trait>` and still be async.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
