//! Terminal escape sanitization, cell-width clipping, and OSC 52 clipboard encoding.

use base64::Engine;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// Strip ANSI CSI, OSC, DCS/SOS/PM/APC, 2-byte ESC sequences, and all C0/C1/DEL control chars.
/// When `keep_newlines` is true, `\n` is preserved (with `\r\n` normalized to `\n`).
pub fn strip_controls_opts(input: &str, keep_newlines: bool) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0;

    while i < bytes.len() {
        let b = bytes[i];
        // 7-bit ESC (0x1B)
        if b == 0x1b {
            i += 1;
            if i >= bytes.len() {
                break;
            }
            match bytes[i] {
                // CSI: ESC [ params intermediate final (0x40..=0x7E)
                b'[' => {
                    i += 1;
                    while i < bytes.len() {
                        let c = bytes[i];
                        i += 1;
                        if (0x40..=0x7e).contains(&c) {
                            break;
                        }
                    }
                }
                // OSC: ESC ] ... (BEL 0x07 or ST ESC \)
                b']' => {
                    i += 1;
                    while i < bytes.len() {
                        if bytes[i] == 0x07 {
                            i += 1;
                            break;
                        }
                        if bytes[i] == 0x1b && i + 1 < bytes.len() && bytes[i + 1] == b'\\' {
                            i += 2;
                            break;
                        }
                        i += 1;
                    }
                }
                // DCS (P), SOS (X), PM (^), APC (_) terminated by ST (ESC \)
                b'P' | b'X' | b'^' | b'_' => {
                    i += 1;
                    while i < bytes.len() {
                        if bytes[i] == 0x1b && i + 1 < bytes.len() && bytes[i + 1] == b'\\' {
                            i += 2;
                            break;
                        }
                        i += 1;
                    }
                }
                // Intermediate bytes (0x20..=0x2F) followed by final byte
                0x20..=0x2f => {
                    i += 1;
                    while i < bytes.len() && (0x20..=0x2f).contains(&bytes[i]) {
                        i += 1;
                    }
                    if i < bytes.len() {
                        i += 1;
                    }
                }
                // Standard 2-byte ESC sequence
                _ => {
                    i += 1;
                }
            }
            continue;
        }

        // Decode next UTF-8 char from remaining valid slice.
        let ch = input[i..].chars().next().unwrap();
        let ch_len = ch.len_utf8();
        i += ch_len;

        // 8-bit CSI (U+009B)
        if ch == '\u{009b}' {
            while i < bytes.len() {
                let c = bytes[i];
                i += 1;
                if (0x40..=0x7e).contains(&c) {
                    break;
                }
            }
            continue;
        }
        // 8-bit OSC (U+009D)
        if ch == '\u{009d}' {
            while i < bytes.len() {
                if bytes[i] == 0x07 {
                    i += 1;
                    break;
                }
                if bytes[i] == 0x1b && i + 1 < bytes.len() && bytes[i + 1] == b'\\' {
                    i += 2;
                    break;
                }
                let next_ch = input[i..].chars().next().unwrap();
                i += next_ch.len_utf8();
                if next_ch == '\u{009c}' {
                    break;
                }
            }
            continue;
        }

        match ch {
            '\n' if keep_newlines => out.push('\n'),
            '\r' => {
                if keep_newlines && i < bytes.len() && bytes[i] == b'\n' {
                    // Skip CR before LF
                } else if !keep_newlines {
                    out.push(' ');
                }
            }
            '\t' => out.push(' '),
            c if c.is_control() => {
                // Drop remaining C0, DEL, and C1 control characters.
            }
            c => out.push(c),
        }
    }

    out
}

/// Sanitize arbitrary untrusted text into a single display-safe line.
pub fn single_line(input: &str) -> String {
    let cleaned = strip_controls_opts(input, false);
    let mut out = String::with_capacity(cleaned.len());
    let mut prev_space = false;
    for ch in cleaned.chars() {
        if ch.is_whitespace() {
            if !prev_space && !out.is_empty() {
                out.push(' ');
                prev_space = true;
            }
        } else {
            out.push(ch);
            prev_space = false;
        }
    }
    if out.ends_with(' ') {
        out.pop();
    }
    out
}

/// Sanitize arbitrary untrusted text while keeping line breaks intact.
pub fn multiline(input: &str) -> String {
    strip_controls_opts(input, true)
}

/// Clip a sanitized string to at most `max_width` terminal display cells.
pub fn clip_width(input: &str, max_width: usize) -> String {
    if max_width == 0 {
        return String::new();
    }
    if input.width() <= max_width {
        return input.to_string();
    }
    if max_width == 1 {
        return "…".to_string();
    }
    let budget = max_width - 1;
    let mut out = String::new();
    let mut used = 0;
    for ch in input.chars() {
        let w = ch.width().unwrap_or(0);
        if used + w > budget {
            break;
        }
        out.push(ch);
        used += w;
    }
    out.push('…');
    out
}

/// Encode text into an OSC 52 clipboard escape sequence (with tmux DCS passthrough when inside tmux).
pub fn osc52_sequence(text: &str, in_tmux: bool) -> String {
    let encoded = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
    if in_tmux {
        format!("\x1bPtmux;\x1b\x1b]52;c;{encoded}\x07\x1b\\")
    } else {
        format!("\x1b]52;c;{encoded}\x07")
    }
}

/// Compute lowercase hex SHA-256 digest (FIPS 180-4) without extra crate dependencies.
pub fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut msg = Vec::with_capacity((data.len() + 9).div_ceil(64) * 64);
    msg.extend_from_slice(data);
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    let (chunks, _) = msg.as_chunks::<64>();
    for chunk in chunks {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[i * 4],
                chunk[i * 4 + 1],
                chunk[i * 4 + 2],
                chunk[i * 4 + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);

            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }

        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    let mut out = String::with_capacity(64);
    for word in h {
        use std::fmt::Write;
        let _ = write!(&mut out, "{word:08x}");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_matches_standard_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn strips_csi_osc_dcs_and_c0_c1_controls() {
        let malicious = "hello\x1b[31m red\x1b[0m \x1b]52;c;YWJj\x07\x1b]0;pwn\x1b\\ok\x1bPtmux;bad\x1b\\\x00\x07\x7f\u{009b}2Jworld\u{0085}!";
        assert_eq!(single_line(malicious), "hello red okworld!");
    }

    #[test]
    fn multiline_preserves_newlines_and_normalizes_crlf() {
        let text = "line1\r\n\x1b[32mline2\x1b[0m\nline3\x07";
        assert_eq!(multiline(text), "line1\nline2\nline3");
    }

    #[test]
    fn clip_width_respects_unicode_display_cells() {
        assert_eq!(clip_width("abcdef", 4), "abc…");
        assert_eq!(clip_width("abc", 4), "abc");
        assert_eq!(clip_width("日本語テスト", 5), "日本…");
        assert_eq!(clip_width("hello", 1), "…");
        assert_eq!(clip_width("hello", 0), "");
    }

    #[test]
    fn osc52_wraps_for_tmux_when_needed() {
        let plain = osc52_sequence("claive show abc", false);
        assert!(plain.starts_with("\x1b]52;c;"));
        assert!(plain.ends_with('\x07'));

        let tmux = osc52_sequence("claive show abc", true);
        assert!(tmux.starts_with("\x1bPtmux;\x1b\x1b]52;c;"));
        assert!(tmux.ends_with("\x07\x1b\\"));
    }
}
