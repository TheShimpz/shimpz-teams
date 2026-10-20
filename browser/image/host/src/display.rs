//! The Browser's display: KasmVNC's X server, confined to its own user and network namespaces, and the relay that
//! carries one viewer's WebSocket to it (ADR-0117, watching over KasmVNC).
//!
//! KasmVNC always opens a UDP listener on every interface and serves an HTTP API on its WebSocket port. Started in a
//! network namespace of its own, whose only interface is its own loopback, nothing of it is reachable from the
//! container's network; X clients reach the display through the filesystem socket. The relay enters those namespaces,
//! connects to the one WebSocket port, drops the capabilities entering them gave it, and copies bytes between its
//! standard streams and that connection until either side ends.

use std::ffi::{c_char, c_int, c_ulong, c_void};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, Shutdown, SocketAddr, TcpStream};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;
use std::thread;
use std::time::Duration;

pub const DISPLAY_SERVER: &str = "/usr/bin/Xkasmvnc";
/// The display's filesystem socket: abstract sockets belong to a network namespace, files do not.
pub const X_SOCKET: &str = "/tmp/.X11-unix/X99";
/// The confined display server's process id, which the relay reads to enter its namespaces.
pub const PID_FILE: &str = "/tmp/run/display.pid";
pub const PORT: u16 = 6901;
const CONNECT_DEADLINE: Duration = Duration::from_secs(5);
const CHUNK: usize = 64 * 1024;

const CLONE_NEWUSER: c_int = 0x1000_0000;
const CLONE_NEWNET: c_int = 0x4000_0000;
const O_WRONLY: c_int = 0o1;
const O_CLOEXEC: c_int = 0o2_000_000;
const AF_INET: c_int = 2;
const SOCK_DGRAM: c_int = 2;
const SIOCGIFFLAGS: c_ulong = 0x8913;
const SIOCSIFFLAGS: c_ulong = 0x8914;
const IFF_UP: i16 = 0x1;
const CAPABILITY_VERSION_3: u32 = 0x2008_0522;
const PR_CAP_AMBIENT: c_int = 47;
const PR_CAP_AMBIENT_CLEAR_ALL: c_ulong = 4;

#[repr(C)]
struct CapabilityHeader {
    version: u32,
    pid: c_int,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CapabilityData {
    effective: u32,
    permitted: u32,
    inheritable: u32,
}

unsafe extern "C" {
    fn unshare(flags: c_int) -> c_int;
    fn setns(fd: c_int, nstype: c_int) -> c_int;
    fn open(path: *const c_char, flags: c_int, ...) -> c_int;
    fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
    fn close(fd: c_int) -> c_int;
    fn socket(domain: c_int, kind: c_int, protocol: c_int) -> c_int;
    fn ioctl(fd: c_int, request: c_ulong, ...) -> c_int;
    fn getuid() -> u32;
    fn getgid() -> u32;
    fn capset(header: *mut CapabilityHeader, data: *const CapabilityData) -> c_int;
    fn prctl(option: c_int, ...) -> c_int;
}

/// Where Team writes a controlled Browser's viewer credentials (`browser-host credentials`).
pub const PASSWORD_FILE: &str = "/tmp/run/kasmpasswd";

/// The display server's arguments: one 1280x800 screen, the WebSocket port on its own loopback only, and every
/// clipboard and resize from a viewer refused. The server's own 1-bit pointer is not drawn (the Cua Driver draws its
/// smooth agent cursor), and every update is sent at the top JPEG quality: measured while the Driver scrolled a long
/// page for two viewers, it cost 30% more bytes (about 500 KiB/s per viewer) and no more CPU than qualities 7 to 9.
///
/// An agent's Browser authenticates no viewer (only the relay reaches it) and refuses every input. A Browser a person
/// may control authenticates every viewer against the credentials Team writes, and applies pointer and key input only
/// from the read-write one (ADR-0117, Acessos amendment item 3); until Team writes them, every viewer is refused.
pub fn arguments(controlled: bool) -> Vec<&'static str> {
    let mut arguments = vec![
        ":99",
        "-geometry",
        "1280x800",
        "-nocursor",
        "-depth",
        "24",
        "-nolisten",
        "tcp",
        "-publicIP",
        "127.0.0.1",
        "-interface",
        "127.0.0.1",
        "-websocketPort",
        "6901",
        "-DisableBasicAuth",
        "1",
        "-SecurityTypes",
        "None",
        "-sslOnly",
        "0",
        "-FrameRate",
        "60",
        "-RectThreads",
        "0",
        "-DynamicQualityMin",
        "9",
        "-DynamicQualityMax",
        "9",
        "-AcceptPointerEvents",
        "0",
        "-AcceptKeyEvents",
        "0",
        "-AcceptSetDesktopSize",
        "0",
        "-AcceptCutText",
        "0",
        "-SendCutText",
        "0",
        "-SendPrimary",
        "0",
        "-SetPrimary",
        "0",
        "-AlwaysShared",
        "1",
        "-DisconnectClients",
        "0",
    ];
    if controlled {
        let set = |arguments: &mut Vec<&'static str>, name: &str, value: &'static str| {
            let index = arguments
                .iter()
                .position(|argument| *argument == name)
                .unwrap();
            arguments[index + 1] = value;
        };
        set(&mut arguments, "-DisableBasicAuth", "0");
        set(&mut arguments, "-AcceptPointerEvents", "1");
        set(&mut arguments, "-AcceptKeyEvents", "1");
        arguments.extend(["-KasmPasswordFile", PASSWORD_FILE]);
    }
    arguments
}

/// Start `command` confined: in a new user namespace mapping only this uid and gid, and a new network namespace
/// whose loopback is up. Any failure stops the child before it executes anything.
pub fn confine(command: &mut Command) {
    // SAFETY: the closure runs between fork and exec and calls only async-signal-safe system calls on stack data.
    unsafe {
        command.pre_exec(|| confined().map_err(io::Error::from_raw_os_error));
    }
}

fn confined() -> Result<(), i32> {
    // SAFETY: plain system calls on values this function owns.
    unsafe {
        let (uid, gid) = (getuid(), getgid());
        succeeded(unshare(CLONE_NEWUSER | CLONE_NEWNET))?;
        let mut line = [0u8; 32];
        write_file(c"/proc/self/setgroups", b"deny")?;
        write_file(c"/proc/self/uid_map", mapping(&mut line, uid))?;
        write_file(c"/proc/self/gid_map", mapping(&mut line, gid))?;
        loopback_up()
    }
}

/// `"<id> <id> 1"` in `buffer`: this one id maps to itself.
fn mapping(buffer: &mut [u8; 32], id: u32) -> &[u8] {
    let mut digits = [0u8; 10];
    let mut count = 0;
    let mut rest = id;
    loop {
        digits[count] = b'0' + (rest % 10) as u8;
        count += 1;
        rest /= 10;
        if rest == 0 {
            break;
        }
    }
    let mut length = 0;
    for _ in 0..2 {
        for index in (0..count).rev() {
            buffer[length] = digits[index];
            length += 1;
        }
        buffer[length] = b' ';
        length += 1;
    }
    buffer[length] = b'1';
    &buffer[..=length]
}

/// # Safety
/// Only async-signal-safe calls.
unsafe fn write_file(path: &std::ffi::CStr, contents: &[u8]) -> Result<(), i32> {
    // SAFETY: the path is NUL-terminated and the buffer outlives the call.
    unsafe {
        let fd = open(path.as_ptr(), O_WRONLY | O_CLOEXEC);
        succeeded(fd)?;
        let written = write(fd, contents.as_ptr().cast(), contents.len());
        close(fd);
        if written == contents.len() as isize {
            Ok(())
        } else {
            Err(5)
        }
    }
}

/// # Safety
/// Only async-signal-safe calls.
unsafe fn loopback_up() -> Result<(), i32> {
    // SAFETY: `request` is a zeroed `struct ifreq` (40 bytes on Linux) naming `lo`; the flags are its short at 16.
    unsafe {
        let fd = socket(AF_INET, SOCK_DGRAM, 0);
        succeeded(fd)?;
        let mut request = [0u8; 40];
        request[..2].copy_from_slice(b"lo");
        let read = ioctl(fd, SIOCGIFFLAGS, request.as_mut_ptr());
        let flags = i16::from_ne_bytes([request[16], request[17]]) | IFF_UP;
        request[16..18].copy_from_slice(&flags.to_ne_bytes());
        let set = if read == 0 {
            ioctl(fd, SIOCSIFFLAGS, request.as_mut_ptr())
        } else {
            read
        };
        close(fd);
        succeeded(set)
    }
}

fn succeeded(result: c_int) -> Result<(), i32> {
    if result < 0 {
        Err(io::Error::last_os_error().raw_os_error().unwrap_or(1))
    } else {
        Ok(())
    }
}

/// Record the confined display server's process id for the relay.
pub fn record(pid: u32) -> Result<(), String> {
    fs::write(PID_FILE, pid.to_string())
        .map_err(|error| format!("cannot record the display: {error}"))
}

/// Carry one viewer's connection: enter the display's namespaces, connect to its WebSocket port, drop every
/// capability, then copy standard input to the connection and the connection to standard output until either ends.
pub fn relay() -> Result<(), String> {
    let pid = display_pid()?;
    let user = namespace(pid, "user")?;
    let net = namespace(pid, "net")?;
    // SAFETY: the process is still single-threaded, as entering a user namespace requires.
    unsafe {
        succeeded(setns(user.as_raw_fd(), CLONE_NEWUSER))
            .map_err(|_| "cannot enter the display's user namespace")?;
        succeeded(setns(net.as_raw_fd(), CLONE_NEWNET))
            .map_err(|_| "cannot enter the display's network namespace")?;
    }
    drop((user, net));
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, PORT));
    let connection = TcpStream::connect_timeout(&address, CONNECT_DEADLINE)
        .map_err(|error| format!("cannot reach the display: {error}"))?;
    drop_capabilities()?;
    connection
        .set_nodelay(true)
        .map_err(|error| format!("cannot configure the connection: {error}"))?;
    let upstream = connection
        .try_clone()
        .map_err(|error| format!("cannot share the connection: {error}"))?;
    thread::spawn(move || {
        copy(io::stdin().lock(), &upstream);
        let _ = upstream.shutdown(Shutdown::Write);
        // The viewer is gone: nothing more is carried.
        std::process::exit(0);
    });
    copy(&connection, io::stdout().lock());
    Ok(())
}

fn display_pid() -> Result<u32, String> {
    let recorded =
        fs::read_to_string(PID_FILE).map_err(|_| "the display is not running".to_owned())?;
    let pid = recorded
        .trim()
        .parse::<u32>()
        .ok()
        .filter(|pid| *pid > 1)
        .ok_or("the display's record is invalid")?;
    let program = fs::read_link(format!("/proc/{pid}/exe"))
        .map_err(|_| "the display is not running".to_owned())?;
    if program != Path::new(DISPLAY_SERVER) {
        return Err("the recorded process is not the display".into());
    }
    Ok(pid)
}

fn namespace(pid: u32, kind: &str) -> Result<File, String> {
    File::open(format!("/proc/{pid}/ns/{kind}"))
        .map_err(|_| format!("cannot open the display's {kind} namespace"))
}

/// Clear every capability entering the display's user namespace gave this process.
fn drop_capabilities() -> Result<(), String> {
    let mut header = CapabilityHeader {
        version: CAPABILITY_VERSION_3,
        pid: 0,
    };
    let empty = [CapabilityData {
        effective: 0,
        permitted: 0,
        inheritable: 0,
    }; 2];
    // SAFETY: the header and two data structs follow the kernel's version 3 layout.
    let cleared = unsafe { capset(&mut header, empty.as_ptr()) };
    succeeded(cleared).map_err(|_| "cannot drop the relay's capabilities".to_owned())?;
    // No ambient capability remains either.
    // SAFETY: prctl with integer arguments only.
    let ambient = unsafe {
        prctl(
            PR_CAP_AMBIENT,
            PR_CAP_AMBIENT_CLEAR_ALL,
            0 as c_ulong,
            0 as c_ulong,
            0 as c_ulong,
        )
    };
    if ambient < 0 {
        return Err("cannot clear the relay's ambient capabilities".into());
    }
    Ok(())
}

fn copy(mut from: impl Read, mut to: impl Write) {
    let mut buffer = vec![0u8; CHUNK];
    while let Ok(read) = from.read(&mut buffer) {
        if read == 0
            || to
                .write_all(&buffer[..read])
                .and_then(|()| to.flush())
                .is_err()
        {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_id_maps_only_to_itself() {
        let mut buffer = [0u8; 32];
        assert_eq!(mapping(&mut buffer, 10031), b"10031 10031 1");
        assert_eq!(mapping(&mut buffer, 0), b"0 0 1");
        assert_eq!(mapping(&mut buffer, u32::MAX), b"4294967295 4294967295 1");
    }

    #[test]
    fn the_display_refuses_every_viewer_input_and_listens_on_its_own_loopback() {
        let arguments = arguments(false);
        let value = |name: &str| {
            let index = arguments
                .iter()
                .position(|argument| *argument == name)
                .unwrap();
            arguments[index + 1]
        };
        assert_eq!(value("-DisableBasicAuth"), "1");
        assert!(!arguments.contains(&"-KasmPasswordFile"));
        for refused in [
            "-AcceptPointerEvents",
            "-AcceptKeyEvents",
            "-AcceptSetDesktopSize",
            "-AcceptCutText",
            "-SendCutText",
            "-SendPrimary",
            "-SetPrimary",
        ] {
            assert_eq!(value(refused), "0", "{refused}");
        }
        assert_eq!(value("-interface"), "127.0.0.1");
        assert!(arguments.contains(&"-nocursor"));
        assert_eq!(value("-DynamicQualityMin"), "9");
        assert_eq!(value("-publicIP"), "127.0.0.1");
        assert_eq!(value("-websocketPort"), PORT.to_string());
        assert_eq!(value("-AlwaysShared"), "1");
        assert!(!arguments.contains(&"unix"));
    }

    #[test]
    fn a_controlled_display_authenticates_every_viewer_and_still_refuses_clipboard_and_resize() {
        let arguments = arguments(true);
        let value = |name: &str| {
            let index = arguments
                .iter()
                .position(|argument| *argument == name)
                .unwrap();
            arguments[index + 1]
        };
        assert_eq!(value("-DisableBasicAuth"), "0");
        assert_eq!(value("-KasmPasswordFile"), PASSWORD_FILE);
        assert_eq!(value("-AcceptPointerEvents"), "1");
        assert_eq!(value("-AcceptKeyEvents"), "1");
        for refused in [
            "-AcceptSetDesktopSize",
            "-AcceptCutText",
            "-SendCutText",
            "-SendPrimary",
            "-SetPrimary",
        ] {
            assert_eq!(value(refused), "0", "{refused}");
        }
        assert_eq!(value("-interface"), "127.0.0.1");
    }

    #[test]
    fn copying_ends_with_its_source() {
        let mut out = Vec::new();
        copy(&b"carried"[..], &mut out);
        assert_eq!(out, b"carried");
    }
}
