//! The private bearer token only the Team controller and the Brain runtime share.
//!
//! Team creates it once, at `/run/shimpz-brain-runtime/token` (a `0750` directory and a `0440` file of 64 hexadecimal
//! digits, both in the shared group 10016), then reuses it only while its custody holds. Every read opens it without
//! following a link and checks the opened descriptor, never the path.

use std::fs::{DirBuilder, File};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::Path;

use team_core::entropy::{Entropy, token_hex};
use team_core::secret::Secret;
use team_storage::custody::{self, DirectoryCustody, FileCustody, Mode, current_uid};
use team_storage::durable;

/// The token's path in the Team image.
pub const TOKEN_PATH: &str = "/run/shimpz-brain-runtime/token";
/// The group the Team controller and the Brain runtime share.
pub const TOKEN_GROUP_GID: u32 = 10016;
/// The token's random bytes; it is stored as twice as many hexadecimal digits.
pub const TOKEN_BYTES: usize = 32;
/// The directory's mode.
pub const DIRECTORY_MODE: u32 = 0o750;
/// The token file's mode.
pub const TOKEN_MODE: u32 = 0o440;

/// Why the token could not be created or trusted; it never carries the token or a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TokenError {
    /// The directory exists outside its custody.
    DirectoryUnsafe,
    /// The directory could not be created.
    DirectoryNotCreated,
    /// The directory could not be opened.
    DirectoryUnavailable,
    /// The token exists outside its custody.
    Unsafe,
    /// The token could not be opened or read.
    Unavailable,
    /// The token is not 64 hexadecimal digits.
    Invalid,
    /// The token could not be created.
    NotCreated,
}

impl std::fmt::Display for TokenError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::DirectoryUnsafe => "the Brain runtime token directory has unsafe metadata",
            Self::DirectoryNotCreated => "the Brain runtime token directory could not be created",
            Self::DirectoryUnavailable => {
                "the Brain runtime token directory is unavailable or unsafe"
            }
            Self::Unsafe => "the Brain runtime token has unsafe metadata",
            Self::Unavailable => "the Brain runtime token is unavailable or unsafe",
            Self::Invalid => "the Brain runtime token is invalid",
            Self::NotCreated => "the Brain runtime token could not be created",
        })
    }
}

impl std::error::Error for TokenError {}

fn directory_custody(group_id: u32) -> DirectoryCustody {
    DirectoryCustody {
        uid: Some(current_uid()),
        gid: Some(group_id),
        mode: Mode::Exact(DIRECTORY_MODE),
    }
}

fn token_custody(group_id: u32) -> FileCustody {
    let size = (TOKEN_BYTES * 2) as u64;
    FileCustody {
        uid: Some(current_uid()),
        gid: Some(group_id),
        mode: Mode::Exact(TOKEN_MODE),
        size: Some((size, size)),
    }
}

fn prepare_directory(path: &Path, group_id: u32) -> Result<(), TokenError> {
    let facts = match custody::facts(path) {
        Ok(Some(facts)) => facts,
        Ok(None) => {
            create_directory(path, group_id)?;
            custody::facts(path)
                .ok()
                .flatten()
                .ok_or(TokenError::DirectoryNotCreated)?
        }
        Err(_) => return Err(TokenError::DirectoryUnavailable),
    };
    if directory_custody(group_id).holds(&facts) {
        Ok(())
    } else {
        Err(TokenError::DirectoryUnsafe)
    }
}

fn create_directory(path: &Path, group_id: u32) -> Result<(), TokenError> {
    DirBuilder::new()
        .recursive(true)
        .mode(DIRECTORY_MODE)
        .create(path)
        .and_then(|()| std::os::unix::fs::lchown(path, None, Some(group_id)))
        .and_then(|()| {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(DIRECTORY_MODE))
        })
        .map_err(|_| TokenError::DirectoryNotCreated)
}

fn decode(raw: &[u8]) -> Result<Secret, TokenError> {
    if raw.len() != TOKEN_BYTES * 2 || !raw.iter().all(u8::is_ascii_hexdigit) {
        return Err(TokenError::Invalid);
    }
    let text = std::str::from_utf8(raw).map_err(|_| TokenError::Invalid)?;
    Ok(Secret::new(text.to_owned()))
}

fn read_checked(directory: &File, name: &str, group_id: u32) -> Result<Secret, TokenError> {
    let raw = custody::read_at(
        directory,
        name,
        &token_custody(group_id),
        (TOKEN_BYTES * 2) as u64,
    )
    .map_err(|error| match error {
        custody::CustodyError::Unsafe => TokenError::Unsafe,
        _ => TokenError::Unavailable,
    })?
    .ok_or(TokenError::Unavailable)?;
    decode(&raw)
}

fn split(path: &Path) -> Result<(&Path, &str), TokenError> {
    let parent = path.parent().ok_or(TokenError::DirectoryUnavailable)?;
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(TokenError::Unavailable)?;
    Ok((parent, name))
}

fn open_directory(parent: &Path, group_id: u32) -> Result<File, TokenError> {
    let directory =
        custody::open_directory(parent).map_err(|_| TokenError::DirectoryUnavailable)?;
    let facts = directory
        .metadata()
        .map_err(|_| TokenError::DirectoryUnavailable)?;
    if directory_custody(group_id).holds(&custody::Facts::from(&facts)) {
        Ok(directory)
    } else {
        Err(TokenError::DirectoryUnsafe)
    }
}

/// Creates the shared token once, then reuses it only while its custody holds; returns it.
///
/// # Errors
/// [`TokenError`] when the directory or token cannot be created or is outside its custody.
pub fn ensure(path: &Path, group_id: u32, entropy: &dyn Entropy) -> Result<Secret, TokenError> {
    let (parent, name) = split(path)?;
    prepare_directory(parent, group_id)?;
    let directory = open_directory(parent, group_id)?;
    match custody::facts_at(&directory, name) {
        Ok(None) => {
            let token = token_hex(entropy, TOKEN_BYTES).map_err(|_| TokenError::NotCreated)?;
            durable::replace_in_directory(
                &directory,
                name,
                token.as_bytes(),
                TOKEN_MODE,
                Some(group_id),
            )
            .map_err(|_| TokenError::NotCreated)?;
        }
        Ok(Some(facts)) if token_custody(group_id).holds(&facts) => {}
        Ok(Some(_)) => return Err(TokenError::Unsafe),
        Err(_) => return Err(TokenError::Unavailable),
    }
    read_checked(&directory, name, group_id)
}

/// Reads the shared token under its custody, never creating it.
///
/// # Errors
/// [`TokenError`] when it is absent, outside its custody, or invalid.
pub fn read(path: &Path, group_id: u32) -> Result<Secret, TokenError> {
    let (parent, name) = split(path)?;
    let directory = open_directory(parent, group_id)?;
    read_checked(&directory, name, group_id)
}

#[cfg(test)]
mod tests;
