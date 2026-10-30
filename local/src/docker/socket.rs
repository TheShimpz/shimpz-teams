//! The CLI's Docker socket probes, run inside the Team image as the controller's own identity (`socket-probe`): the
//! socket's group, and whether the daemon answers `GET /_ping` through it.

use std::io::{Read as _, Write as _};
use std::os::unix::fs::{FileTypeExt as _, MetadataExt as _};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

/// How long the ping waits to connect, send, and read.
const PING_TIMEOUT: Duration = Duration::from_secs(5);
/// The most of the answer the ping reads.
const PING_READ: usize = 128;

/// The group of the socket at `path` (its target when it is a link), when it is a socket.
#[must_use]
pub fn gid(path: &Path) -> Option<u32> {
    let metadata = std::fs::metadata(path).ok()?;
    metadata.file_type().is_socket().then(|| metadata.gid())
}

/// Whether the daemon at `path` answers `GET /_ping` with `200 OK` in the first line of one read of its answer.
#[must_use]
pub fn ping(path: &Path) -> bool {
    let answered = || -> std::io::Result<Vec<u8>> {
        let mut stream = UnixStream::connect(path)?;
        stream.set_read_timeout(Some(PING_TIMEOUT))?;
        stream.set_write_timeout(Some(PING_TIMEOUT))?;
        stream.write_all(b"GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n")?;
        let mut answer = vec![0; PING_READ];
        let read = stream.read(&mut answer)?;
        answer.truncate(read);
        Ok(answer)
    };
    answered().is_ok_and(|answer| {
        let line = answer
            .windows(2)
            .position(|pair| pair == b"\r\n")
            .map_or(answer.as_slice(), |end| &answer[..end]);
        line == b"HTTP/1.0 200 OK" || line == b"HTTP/1.1 200 OK"
    })
}

#[cfg(test)]
mod tests {
    use std::io::{Read as _, Write as _};
    use std::os::unix::fs::MetadataExt as _;
    use std::os::unix::net::UnixListener;
    use std::path::Path;

    use super::{gid, ping};

    fn answering(answer: &'static [u8]) -> bool {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let path = directory.path().join("docker.sock");
        let listener = UnixListener::bind(&path).expect("bound");
        let daemon = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("one connection");
            let mut request = [0; 64];
            let read = stream.read(&mut request).expect("the request");
            stream.write_all(answer).expect("the answer");
            request[..read].to_vec()
        });
        let healthy = ping(&path);
        let request = daemon.join().expect("the daemon");
        assert!(request.starts_with(b"GET /_ping HTTP/1.0\r\n"));
        healthy
    }

    #[test]
    fn only_a_200_first_line_is_a_healthy_daemon() {
        assert!(answering(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"));
        assert!(answering(b"HTTP/1.0 200 OK\r\n\r\nOK"));
        assert!(!answering(b"HTTP/1.1 500 Internal Server Error\r\n\r\n"));
        assert!(!answering(b"HTTP/1.1 200 OKAY\r\n\r\n"));
        assert!(!ping(Path::new("/nonexistent/docker.sock")));
    }

    #[test]
    fn only_a_socket_has_a_group() {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let path = directory.path().join("docker.sock");
        let _listener = UnixListener::bind(&path).expect("bound");
        assert_eq!(
            gid(&path),
            Some(std::fs::metadata(&path).expect("stat").gid())
        );
        let link = directory.path().join("link.sock");
        std::os::unix::fs::symlink(&path, &link).expect("linked");
        assert_eq!(gid(&link), gid(&path));
        let file = directory.path().join("file");
        std::fs::write(&file, b"").expect("written");
        assert_eq!(gid(&file), None);
        assert_eq!(gid(&directory.path().join("missing")), None);
    }
}
