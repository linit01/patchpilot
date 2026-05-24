import Foundation

/// Parses backend timestamp strings and renders them in the phone's local timezone.
///
/// Backend wire format is Python's `str(datetime_with_tz)`, e.g.
/// `"2026-05-21 17:02:34.123456+00:00"` (space separator, microseconds, offset).
/// Also tolerates ISO8601 variants in case other endpoints send them.
enum BackendDate {
    private static let parsers: [DateFormatter] = {
        let patterns = [
            "yyyy-MM-dd HH:mm:ss.SSSSSSxxx",
            "yyyy-MM-dd HH:mm:ssxxx",
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxx",
            "yyyy-MM-dd'T'HH:mm:ssxxx",
            "yyyy-MM-dd HH:mm:ss.SSSSSS",
            "yyyy-MM-dd HH:mm:ss",
        ]
        return patterns.map { pattern in
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = TimeZone(identifier: "UTC")
            f.dateFormat = pattern
            return f
        }
    }()

    static func parse(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        for parser in parsers {
            if let d = parser.date(from: raw) { return d }
        }
        return nil
    }

    /// Render in phone-local zone, abbreviated date + short time (e.g. "May 21, 2026 at 10:02 AM").
    /// Falls back to the raw string if parsing fails, so we never hide data.
    static func formatLocal(_ raw: String?, fallback: String = "—") -> String {
        guard let raw, !raw.isEmpty else { return fallback }
        guard let date = parse(raw) else { return raw }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
