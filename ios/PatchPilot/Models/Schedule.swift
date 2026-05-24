import Foundation

struct Schedule: Codable, Identifiable {
    let id: String
    let name: String
    let enabled: Bool
    let dayOfWeek: String
    let startTime: String
    let endTime: String
    let timezone: String?
    let autoReboot: Bool
    let hostIds: [String]?
    let createdBy: String?

    enum CodingKeys: String, CodingKey {
        case id, name, enabled, timezone
        case dayOfWeek = "day_of_week"
        case startTime = "start_time"
        case endTime = "end_time"
        case autoReboot = "auto_reboot"
        case hostIds = "host_ids"
        case createdBy = "created_by"
    }

    var daysDisplay: String {
        dayOfWeek
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces).capitalized }
            .joined(separator: ", ")
    }

    /// "02:00 – 04:00 MDT" — start_time/end_time are wall-clock in the server's
    /// configured schedule_timezone, so we tag the window with that zone's
    /// abbreviation. Falls back to the IANA name if no abbreviation is available,
    /// and omits the tag entirely if the backend didn't send a zone (old server).
    var timeWindowDisplay: String {
        let base = "\(startTime) – \(endTime)"
        guard let tz = timezone, !tz.isEmpty else { return base }
        let label = TimeZone(identifier: tz)?.abbreviation() ?? tz
        return "\(base) \(label)"
    }
}

struct ScheduleCreateRequest: Encodable {
    let name: String
    let enabled: Bool
    let dayOfWeek: String
    let startTime: String
    let endTime: String
    let autoReboot: Bool
    let becomePassword: String?
    let hostIds: [String]

    enum CodingKeys: String, CodingKey {
        case name, enabled
        case dayOfWeek = "day_of_week"
        case startTime = "start_time"
        case endTime = "end_time"
        case autoReboot = "auto_reboot"
        case becomePassword = "become_password"
        case hostIds = "host_ids"
    }
}
