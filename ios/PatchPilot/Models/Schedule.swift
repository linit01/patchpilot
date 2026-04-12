import Foundation

struct Schedule: Codable, Identifiable {
    let id: String
    let name: String
    let enabled: Bool
    let dayOfWeek: String
    let startTime: String
    let endTime: String
    let autoReboot: Bool
    let hostIds: [String]?
    let createdBy: String?

    enum CodingKeys: String, CodingKey {
        case id, name, enabled
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

    var timeWindowDisplay: String {
        "\(startTime) – \(endTime)"
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
