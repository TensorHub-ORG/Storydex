use std::cmp::Ordering;
use std::path::Path;

/// Chapter directories and flat manuscripts share the same indexing contract.
pub fn is_chapter_entry(name: &str, is_directory: bool) -> bool {
    let path = Path::new(name);
    let stem = if is_directory {
        name
    } else {
        path.file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("")
    };
    if name.starts_with('.')
        || matches!(
            stem.to_ascii_lowercase().as_str(),
            "readme" | "index" | "summary" | "outline" | "目录" | "说明" | "章纲" | "大纲"
        )
    {
        return false;
    }
    is_directory
        || path
            .extension()
            .and_then(|value| value.to_str())
            .is_some_and(|extension| {
                extension.eq_ignore_ascii_case("md") || extension.eq_ignore_ascii_case("txt")
            })
}

pub fn chapter_number(name: &str) -> Option<u32> {
    let lower = name.trim().to_ascii_lowercase();
    let prefix = lower
        .strip_prefix('第')
        .or_else(|| lower.strip_prefix("chapter"))
        .or_else(|| lower.strip_prefix("ch"))
        .unwrap_or(&lower)
        .trim_start();
    let digits: String = prefix.chars().take_while(char::is_ascii_digit).collect();
    if !digits.is_empty() {
        return digits.parse().ok();
    }
    let numerals: String = prefix
        .chars()
        .take_while(|value| "零〇一二两三四五六七八九十百千万".contains(*value))
        .collect();
    if numerals.is_empty() || !prefix[numerals.len()..].starts_with('章') {
        return None;
    }
    let mut total = 0_u32;
    let mut section = 0_u32;
    let mut digit = 0_u32;
    for value in numerals.chars() {
        match value {
            '十' | '百' | '千' => {
                let unit = match value {
                    '十' => 10,
                    '百' => 100,
                    _ => 1000,
                };
                section = section.checked_add(digit.max(1).checked_mul(unit)?)?;
                digit = 0;
            }
            '万' => {
                total = total.checked_add(section.checked_add(digit)?.checked_mul(10_000)?)?;
                section = 0;
                digit = 0;
            }
            _ => {
                let value = match value {
                    '零' | '〇' => 0,
                    '一' => 1,
                    '二' | '两' => 2,
                    '三' => 3,
                    '四' => 4,
                    '五' => 5,
                    '六' => 6,
                    '七' => 7,
                    '八' => 8,
                    '九' => 9,
                    _ => unreachable!(),
                };
                digit = digit.checked_mul(10)?.checked_add(value)?;
            }
        }
    }
    total.checked_add(section)?.checked_add(digit)
}

pub fn compare_chapter_paths(left: &str, right: &str) -> Ordering {
    for (left, right) in left.split('/').zip(right.split('/')) {
        let left_number = chapter_number(left);
        let right_number = chapter_number(right);
        let order = (left_number.is_none(), left_number, left).cmp(&(
            right_number.is_none(),
            right_number,
            right,
        ));
        if order != Ordering::Equal {
            return order;
        }
    }
    left.cmp(right)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chapter_names_use_leading_arabic_or_chinese_numbers() {
        for (name, expected) in [
            ("第一章-显影.md", Some(1)),
            ("第十二章", Some(12)),
            ("第一百零二章", Some(102)),
            ("第一千零二十三章", Some(1023)),
            ("第两百章", Some(200)),
            ("第一万零三章", Some(10003)),
            ("第一〇二章", Some(102)),
            ("第10章", Some(10)),
            ("001.md", Some(1)),
            ("Chapter 20 - Dawn", Some(20)),
            ("重返1934.md", None),
            ("一场雨.md", None),
        ] {
            assert_eq!(chapter_number(name), expected, "{name}");
        }
    }
}
