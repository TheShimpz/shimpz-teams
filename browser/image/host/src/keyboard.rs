//! The display's keyboard map for a Browser a person drives (ADR-0117, Sign-ins amendment item 3).
//!
//! The display maps a keysym its US keyboard map lacks (é, ç, ã, ...) onto a spare keycode the first time a viewer
//! sends it, and the client then misses that first key while its keyboard map changes beneath it: measured, a person's
//! first é never reached the page. So before Chromium starts, the accented letters and symbols of the interface
//! languages are given spare keycodes of their own, in the same US map; the display then finds each at once.

use std::io::Write;
use std::process::{Command, Stdio};

/// The keyboard map the display starts with, which every key of the US layout keeps.
const LAYOUT: &str = "us";
/// The symbols include the US map names; the accented keys are added after it.
const ANCHOR: &str = "+inet(evdev)";
const EXTRA: &str = "+inet(evdev)+shimpz(latin)";
/// Spare keycodes the evdev keycodes define one by one (not as aliases) and the US map leaves to media keys:
/// `<I208>` to `<I255>`, the last a core X keycode can be.
const FIRST_SPARE: usize = 208;
#[cfg(test)]
const LAST_SPARE: usize = 255;
/// Each spare key's lower and upper keysym: the Latin letters and signs of the interface languages that a US map lacks.
const KEYS: &[(&str, &str)] = &[
    ("aacute", "Aacute"),
    ("agrave", "Agrave"),
    ("acircumflex", "Acircumflex"),
    ("atilde", "Atilde"),
    ("adiaeresis", "Adiaeresis"),
    ("aring", "Aring"),
    ("ae", "AE"),
    ("ccedilla", "Ccedilla"),
    ("eacute", "Eacute"),
    ("egrave", "Egrave"),
    ("ecircumflex", "Ecircumflex"),
    ("ediaeresis", "Ediaeresis"),
    ("iacute", "Iacute"),
    ("igrave", "Igrave"),
    ("icircumflex", "Icircumflex"),
    ("idiaeresis", "Idiaeresis"),
    ("ntilde", "Ntilde"),
    ("oacute", "Oacute"),
    ("ograve", "Ograve"),
    ("ocircumflex", "Ocircumflex"),
    ("otilde", "Otilde"),
    ("odiaeresis", "Odiaeresis"),
    ("oslash", "Oslash"),
    ("oe", "OE"),
    ("uacute", "Uacute"),
    ("ugrave", "Ugrave"),
    ("ucircumflex", "Ucircumflex"),
    ("udiaeresis", "Udiaeresis"),
    ("yacute", "Yacute"),
    ("ydiaeresis", "Ydiaeresis"),
    ("ssharp", "ssharp"),
    ("EuroSign", "EuroSign"),
    ("questiondown", "exclamdown"),
    ("ordfeminine", "masculine"),
    ("guillemotleft", "guillemotright"),
    ("degree", "section"),
    ("acute", "diaeresis"),
    ("cedilla", "sterling"),
];
const SETXKBMAP: &str = "/usr/bin/setxkbmap";
const XKBCOMP: &str = "/usr/bin/xkbcomp";

/// The `shimpz(latin)` symbols: one spare keycode for each pair of `KEYS`.
pub fn symbols() -> String {
    let keys: String = KEYS
        .iter()
        .enumerate()
        .map(|(index, (lower, upper))| {
            format!(
                "    key <I{}> {{ [ {lower}, {upper} ] }};\n",
                FIRST_SPARE + index
            )
        })
        .collect();
    format!("partial alphanumeric_keys\nxkb_symbols \"latin\" {{\n{keys}}};\n")
}

/// The US keymap `setxkbmap` prints, with the accented keys added after its own symbols.
pub fn extended(keymap: &str) -> Result<String, String> {
    if keymap.matches(ANCHOR).count() != 1 {
        return Err("the display's keyboard map is not the one the accented keys extend".into());
    }
    Ok(keymap.replacen(ANCHOR, EXTRA, 1))
}

/// Load the US keymap with the accented keys into `display`; `include` is the directory holding `symbols/shimpz`.
pub fn load(
    display: &str,
    include: &std::path::Path,
    environment: &[(&str, &str)],
) -> Result<(), String> {
    let symbols_directory = include.join("symbols");
    std::fs::create_dir_all(&symbols_directory)
        .and_then(|()| std::fs::write(symbols_directory.join("shimpz"), symbols()))
        .map_err(|error| format!("cannot write the keyboard symbols: {error}"))?;
    let printed = Command::new(SETXKBMAP)
        .env_clear()
        .envs(environment.iter().copied())
        .args(["-display", display, "-print", "-layout", LAYOUT])
        .output()
        .map_err(|error| format!("cannot read the keyboard map: {error}"))?;
    if !printed.status.success() {
        return Err("cannot read the keyboard map".into());
    }
    let keymap = extended(&String::from_utf8_lossy(&printed.stdout))?;
    let mut compiler = Command::new(XKBCOMP)
        .env_clear()
        .envs(environment.iter().copied())
        .arg("-w0")
        .arg(format!("-I{}", include.display()))
        .args(["-", display])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("cannot load the keyboard map: {error}"))?;
    let written = compiler
        .stdin
        .take()
        .ok_or("cannot load the keyboard map")?
        .write_all(keymap.as_bytes());
    let status = compiler
        .wait()
        .map_err(|error| format!("cannot load the keyboard map: {error}"))?;
    if written.is_err() || !status.success() {
        return Err("cannot load the keyboard map".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_accented_key_has_its_own_spare_keycode() {
        assert!(FIRST_SPARE + KEYS.len() - 1 <= LAST_SPARE);
        let symbols = symbols();
        assert!(symbols.starts_with("partial alphanumeric_keys\nxkb_symbols \"latin\" {\n"));
        assert!(symbols.contains("    key <I208> { [ aacute, Aacute ] };\n"));
        assert!(symbols.contains("    key <I216> { [ eacute, Eacute ] };\n"));
        assert_eq!(symbols.matches("key <I").count(), KEYS.len());
    }

    #[test]
    fn the_accented_keys_extend_exactly_the_us_map() {
        let printed = "xkb_keymap {\n\txkb_symbols { include \"pc+us+inet(evdev)\" };\n};\n";
        assert_eq!(
            extended(printed).unwrap(),
            "xkb_keymap {\n\txkb_symbols { include \"pc+us+inet(evdev)+shimpz(latin)\" };\n};\n"
        );
        assert!(extended("xkb_symbols { include \"pc+us\" };").is_err());
        assert!(extended("+inet(evdev) +inet(evdev)").is_err());
    }
}
