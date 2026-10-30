//! What the controller requires of the daemon before it starts: the default seccomp profile (every workload Team
//! creates relies on it), a CPU count to share half of, and a supported architecture.

use bollard::models::SystemInfo;

use super::{DockerEngine, DockerError};

/// The daemon facts every workload's placement uses.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DaemonPolicy {
    /// The CPUs every workload shares: the first half of the daemon's (`0` or `0-n`).
    pub cpuset: String,
    /// The daemon's platform (`linux/amd64` or `linux/arm64`).
    pub platform: &'static str,
}

/// Why the controller refuses to start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StartRefused {
    /// The daemon could not be reached.
    Unavailable,
    /// The daemon runs without its default seccomp profile.
    NoDefaultSeccomp,
    /// The daemon reported no positive CPU count.
    InvalidCpuCount,
    /// The daemon's architecture is not supported.
    UnsupportedArchitecture,
}

impl std::fmt::Display for StartRefused {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "the Docker daemon is unavailable",
            Self::NoDefaultSeccomp => "the Docker daemon default seccomp profile is required",
            Self::InvalidCpuCount => "the Docker daemon reported an invalid CPU count",
            Self::UnsupportedArchitecture => "the Docker daemon architecture is unsupported",
        })
    }
}

impl std::error::Error for StartRefused {}

/// The first half of `processors` CPUs, at least one.
///
/// # Errors
/// [`StartRefused::InvalidCpuCount`] for a count below one.
pub fn half_cpu_set(processors: i64) -> Result<String, StartRefused> {
    if processors < 1 {
        return Err(StartRefused::InvalidCpuCount);
    }
    let available = (processors / 2).max(1);
    Ok(if available == 1 {
        "0".to_owned()
    } else {
        format!("0-{}", available - 1)
    })
}

/// The policy of a daemon that reported `info`.
///
/// # Errors
/// [`StartRefused`] for a daemon without its default seccomp profile, a CPU count, or a supported architecture.
pub fn admit(info: &SystemInfo) -> Result<DaemonPolicy, StartRefused> {
    let seccomp = info.security_options.as_ref().is_some_and(|options| {
        options
            .iter()
            .any(|option| option.starts_with("name=seccomp"))
    });
    if !seccomp {
        return Err(StartRefused::NoDefaultSeccomp);
    }
    let cpuset = half_cpu_set(info.ncpu.ok_or(StartRefused::InvalidCpuCount)?)?;
    let platform = match info.architecture.as_deref() {
        Some("amd64" | "x86_64") => "linux/amd64",
        Some("arm64" | "aarch64") => "linux/arm64",
        _ => return Err(StartRefused::UnsupportedArchitecture),
    };
    Ok(DaemonPolicy { cpuset, platform })
}

/// Reads the daemon's information and admits it.
///
/// # Errors
/// [`StartRefused`].
pub async fn require(docker: &dyn DockerEngine) -> Result<DaemonPolicy, StartRefused> {
    let info = docker
        .info()
        .await
        .map_err(|_: DockerError| StartRefused::Unavailable)?;
    admit(&info)
}

#[cfg(all(test, feature = "testing"))]
mod tests {
    use bollard::models::SystemInfo;

    use super::{DaemonPolicy, StartRefused, admit, half_cpu_set, require};
    use crate::testing::FakeDocker;

    fn info(options: &[&str], ncpu: Option<i64>, architecture: &str) -> SystemInfo {
        SystemInfo {
            security_options: Some(options.iter().map(|option| (*option).to_owned()).collect()),
            ncpu,
            architecture: Some(architecture.to_owned()),
            ..SystemInfo::default()
        }
    }

    #[tokio::test]
    async fn daemon_without_default_seccomp_refuses_start() {
        let docker = FakeDocker::default();
        docker.set_info(info(&["name=apparmor", "name=cgroupns"], Some(4), "x86_64"));
        assert_eq!(require(&docker).await, Err(StartRefused::NoDefaultSeccomp));
        docker.set_info(info(&[], Some(4), "x86_64"));
        assert_eq!(require(&docker).await, Err(StartRefused::NoDefaultSeccomp));
        docker.set_info(SystemInfo::default());
        assert_eq!(require(&docker).await, Err(StartRefused::NoDefaultSeccomp));
        docker.set_info(info(&["name=seccomp,profile=builtin"], Some(4), "x86_64"));
        assert_eq!(
            require(&docker).await,
            Ok(DaemonPolicy {
                cpuset: "0-1".to_owned(),
                platform: "linux/amd64"
            })
        );
        docker.set_unavailable(true);
        assert_eq!(require(&docker).await, Err(StartRefused::Unavailable));
    }

    #[test]
    fn workloads_share_half_the_cpus_of_a_supported_architecture() {
        assert_eq!(half_cpu_set(1).as_deref(), Ok("0"));
        assert_eq!(half_cpu_set(3).as_deref(), Ok("0"));
        assert_eq!(half_cpu_set(96).as_deref(), Ok("0-47"));
        assert_eq!(half_cpu_set(0), Err(StartRefused::InvalidCpuCount));
        let seccomp = ["name=seccomp,profile=builtin"];
        assert_eq!(
            admit(&info(&seccomp, None, "x86_64")),
            Err(StartRefused::InvalidCpuCount)
        );
        assert_eq!(
            admit(&info(&seccomp, Some(2), "aarch64")).map(|policy| policy.platform),
            Ok("linux/arm64")
        );
        assert_eq!(
            admit(&info(&seccomp, Some(2), "riscv64")),
            Err(StartRefused::UnsupportedArchitecture)
        );
    }
}
