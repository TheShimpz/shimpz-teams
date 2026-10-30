//! Where a Browser task's downloads go (`local/browser/service.py` `_ingest`): the Team's files, marked as from the
//! web, each with the media type Team detects from its bytes.

use std::sync::Arc;

use strict_json::{Number, Value};
use team_browser::ingest::PutRefused;
use team_core::future::BoxFuture;
use team_storage::files::{FileStore, StorageErrorKind};

use crate::browser::service::WebFiles;

/// The Team files as a Browser task's download sink.
#[derive(Clone)]
pub struct Downloads(pub Arc<FileStore>);

impl std::fmt::Debug for Downloads {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Downloads")
    }
}

impl WebFiles for Downloads {
    fn put<'a>(
        &'a self,
        team_id: &'a str,
        name: &'a str,
        data: Vec<u8>,
    ) -> BoxFuture<'a, Result<Value, PutRefused>> {
        let (files, team, name) = (Arc::clone(&self.0), team_id.to_owned(), name.to_owned());
        Box::pin(async move {
            let stored = tokio::task::spawn_blocking(move || {
                let media_type = team_prepare::detect::detect(&name, &data).media_type;
                files.put(&team, &name, &data, Some(media_type), true)
            })
            .await
            .map_err(|_| PutRefused::Unavailable)?;
            let upload = stored.map_err(|error| match error.kind {
                StorageErrorKind::Quota => PutRefused::QuotaExceeded,
                StorageErrorKind::Input => PutRefused::NameRefused,
                _ => PutRefused::Unavailable,
            })?;
            let metadata = &upload.file.metadata;
            Ok(Value::Object(vec![
                ("id".to_owned(), Value::String(metadata.id.clone())),
                ("name".to_owned(), Value::String(metadata.name.clone())),
                (
                    "media_type".to_owned(),
                    Value::String(metadata.media_type.clone()),
                ),
                ("size".to_owned(), Value::Number(Number::Int(metadata.size))),
            ]))
        })
    }
}
