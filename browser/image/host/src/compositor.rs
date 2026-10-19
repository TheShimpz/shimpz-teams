//! The display's compositor (ADR-0117): xcompmgr, so the Cua Driver's agent cursor blends over the page as the page
//! changes beneath it. Without one, the cursor's overlay keeps a halo of whatever it was first drawn over.
//!
//! The compositor is ready once it owns the screen's `_NET_WM_CM_S0` selection, the compositing manager's selection
//! (EWMH), read over the display's filesystem socket with two X11 core requests and no authorization.

use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

pub const COMPOSITOR: &str = "/usr/bin/xcompmgr";
/// Plain compositing: no shadows or fades to draw.
pub const ARGUMENTS: &[&str] = &["-n"];
const SELECTION: &[u8] = b"_NET_WM_CM_S0";
/// One check, connection to answer, ends by this bound however slowly the display answers.
const CHECK: Duration = Duration::from_secs(2);
/// The connection setup's extra data the check reads at most; a display's is a few kilobytes.
const MAX_SETUP_BYTES: usize = 1024 * 1024;
const INTERN_ATOM: u8 = 16;
const GET_SELECTION_OWNER: u8 = 23;

/// Whether a compositor owns the display's compositing selection now, answered within CHECK.
pub fn active(socket: &str) -> bool {
    let deadline = Instant::now() + CHECK;
    connect(socket, deadline)
        .and_then(|stream| selection_owned(Bounded { stream, deadline }, SELECTION))
        .unwrap_or(false)
}

/// Connect to `socket` by `deadline`: a display whose accept queue is full blocks a connection, which is then given
/// up on and finishes, or not, on its own thread.
fn connect(socket: &str, deadline: Instant) -> io::Result<UnixStream> {
    let (sender, receiver) = mpsc::channel();
    let socket = socket.to_owned();
    thread::spawn(move || {
        let _ = sender.send(UnixStream::connect(socket));
    });
    receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .unwrap_or_else(|_| {
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "the display did not accept in time",
            ))
        })
}

/// A display connection whose every read and write waits only for what is left of one absolute deadline.
struct Bounded {
    stream: UnixStream,
    deadline: Instant,
}

impl Bounded {
    fn left(&self) -> io::Result<Duration> {
        let left = self.deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "the display did not answer in time",
            ));
        }
        Ok(left)
    }
}

impl Read for Bounded {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.stream.set_read_timeout(Some(self.left()?))?;
        self.stream.read(buffer)
    }
}

impl Write for Bounded {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.stream.set_write_timeout(Some(self.left()?))?;
        self.stream.write(data)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.stream.flush()
    }
}

/// Ask an X server, over a fresh connection, whether `selection` has an owner.
pub fn selection_owned(mut stream: impl Read + Write, selection: &[u8]) -> io::Result<bool> {
    // Connection setup: little-endian, protocol 11.0, no authorization name or data.
    stream.write_all(&[b'l', 0, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0])?;
    let mut head = [0u8; 8];
    stream.read_exact(&mut head)?;
    let extra = usize::from(u16::from_le_bytes([head[6], head[7]])) * 4;
    if head[0] != 1 || extra > MAX_SETUP_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "the display refused the connection",
        ));
    }
    io::copy(&mut (&mut stream).take(extra as u64), &mut io::sink())?;
    stream.write_all(&intern_atom(selection))?;
    let atom = reply(&mut stream)?;
    let mut owner = vec![GET_SELECTION_OWNER, 0, 2, 0];
    owner.extend(atom.to_le_bytes());
    stream.write_all(&owner)?;
    Ok(reply(&mut stream)? != 0)
}

/// InternAtom of `name`, creating it if absent.
fn intern_atom(name: &[u8]) -> Vec<u8> {
    let padded = name.len().div_ceil(4) * 4;
    let units = u16::try_from((8 + padded) / 4).unwrap_or(u16::MAX);
    let length = u16::try_from(name.len()).unwrap_or(u16::MAX);
    let mut request = vec![INTERN_ATOM, 0];
    request.extend(units.to_le_bytes());
    request.extend(length.to_le_bytes());
    request.extend([0, 0]);
    request.extend(name);
    request.resize(8 + padded, 0);
    request
}

/// The 32-bit value of a one-word reply; an X error or event is no answer.
fn reply(stream: &mut impl Read) -> io::Result<u32> {
    let mut answer = [0u8; 32];
    stream.read_exact(&mut answer)?;
    if answer[0] != 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "the display answered with an error",
        ));
    }
    Ok(u32::from_le_bytes([
        answer[8], answer[9], answer[10], answer[11],
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn answer(value: u32) -> [u8; 32] {
        let mut reply = [0u8; 32];
        reply[0] = 1;
        reply[8..12].copy_from_slice(&value.to_le_bytes());
        reply
    }

    /// A display that accepts the setup and answers the atom 0x77 and `owner` as its selection's owner.
    fn display(owner: u32, setup_status: u8) -> (UnixStream, thread::JoinHandle<Vec<u8>>) {
        let (client, mut server) = UnixStream::pair().unwrap();
        let handle = thread::spawn(move || {
            let mut seen = Vec::new();
            let mut setup = [0u8; 12];
            server.read_exact(&mut setup).unwrap();
            seen.extend(setup);
            server
                .write_all(&[setup_status, 0, 11, 0, 0, 0, 1, 0, 9, 9, 9, 9])
                .unwrap();
            if setup_status != 1 {
                return seen;
            }
            let mut intern = [0u8; 24];
            server.read_exact(&mut intern).unwrap();
            seen.extend(intern);
            server.write_all(&answer(0x77)).unwrap();
            let mut get = [0u8; 8];
            server.read_exact(&mut get).unwrap();
            seen.extend(get);
            server.write_all(&answer(owner)).unwrap();
            seen
        });
        (client, handle)
    }

    #[test]
    fn a_selection_with_an_owner_is_compositing_and_one_without_is_not() {
        for (owner, expected) in [(0x0040_0001, true), (0, false)] {
            let (client, handle) = display(owner, 1);
            assert_eq!(selection_owned(client, SELECTION).unwrap(), expected);
            let seen = handle.join().unwrap();
            assert_eq!(&seen[..12], &[b'l', 0, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
            // The X11 core protocol's own opcodes: InternAtom is 16, GetSelectionOwner 23.
            assert_eq!(&seen[12..20], &[16, 0, 6, 0, 13, 0, 0, 0]);
            assert_eq!(&seen[20..33], SELECTION);
            assert_eq!(&seen[36..44], &[23, 0, 2, 0, 0x77, 0, 0, 0]);
        }
    }

    /// A display that accepts the connection, then answers nothing or one byte at a time, forever.
    fn slow_display(drip: bool) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let path =
            std::env::temp_dir().join(format!("compositor-{}-{drip}.sock", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        let handle = thread::spawn(move || {
            let (mut peer, _) = listener.accept().unwrap();
            let mut setup = [0u8; 12];
            let _ = peer.read_exact(&mut setup);
            let mut answer = [1u8, 0, 11, 0, 0, 0, 0, 0].into_iter().cycle();
            for _ in 0..30 {
                thread::sleep(Duration::from_millis(100));
                if drip && peer.write_all(&[answer.next().unwrap()]).is_err() {
                    return;
                }
            }
        });
        (path, handle)
    }

    #[test]
    fn a_display_that_stalls_or_drips_is_given_up_on_at_the_check_bound() {
        for drip in [false, true] {
            let (path, handle) = slow_display(drip);
            let started = Instant::now();
            assert!(!active(path.to_str().unwrap()));
            let took = started.elapsed();
            assert!(
                took >= CHECK - Duration::from_millis(200),
                "{drip}: {took:?}"
            );
            assert!(took < CHECK + Duration::from_secs(1), "{drip}: {took:?}");
            handle.join().unwrap();
            let _ = std::fs::remove_file(&path);
        }
    }

    #[test]
    fn a_display_whose_accept_queue_is_full_is_given_up_on_at_the_check_bound() {
        let path =
            std::env::temp_dir().join(format!("compositor-full-{}.sock", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let _listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        // Fill the queue the listener never accepts from, until one more connection would block.
        let mut held = Vec::new();
        while let Ok(stream) = connect(
            path.to_str().unwrap(),
            Instant::now() + Duration::from_millis(200),
        ) {
            held.push(stream);
            assert!(held.len() < 100_000, "the accept queue never filled");
        }
        let started = Instant::now();
        assert!(!active(path.to_str().unwrap()));
        let took = started.elapsed();
        assert!(took >= CHECK - Duration::from_millis(200), "{took:?}");
        assert!(took < CHECK + Duration::from_secs(1), "{took:?}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn a_refused_connection_or_an_error_answer_is_no_compositor() {
        let (client, handle) = display(1, 0);
        assert!(selection_owned(client, SELECTION).is_err());
        handle.join().unwrap();
        let (client, mut server) = UnixStream::pair().unwrap();
        let handle = thread::spawn(move || {
            let mut setup = [0u8; 12];
            server.read_exact(&mut setup).unwrap();
            server.write_all(&[1, 0, 11, 0, 0, 0, 0, 0]).unwrap();
            let mut intern = [0u8; 24];
            server.read_exact(&mut intern).unwrap();
            server.write_all(&[0u8; 32]).unwrap();
        });
        assert!(selection_owned(client, SELECTION).is_err());
        handle.join().unwrap();
        assert!(!active("/nonexistent/display.sock"));
    }
}
