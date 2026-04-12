import SwiftUI

/// PatchPilot dark theme colors matching the web UI CSS variables
enum Theme {
    // MARK: - Backgrounds
    static let bgBlack = Color(hex: 0x000000)
    static let bgPanel = Color(hex: 0x111111)
    static let bgCard = Color(hex: 0x1A1A1A)
    static let bgCardHover = Color(hex: 0x222222)
    static let bgCardInner = Color(hex: 0x0D0D0D)

    // MARK: - Borders
    static let border = Color(hex: 0x2A2A2A)
    static let borderLight = Color(hex: 0x1E1E1E)

    // MARK: - Text
    static let textPrimary = Color(hex: 0xE8E8E8)
    static let textSecondary = Color(hex: 0x999999)
    static let textMuted = Color(hex: 0x555555)

    // MARK: - Accent Colors
    static let blue = Color(hex: 0x3498DB)
    static let green = Color(hex: 0x00A65A)
    static let greenBright = Color(hex: 0x2ECC71)
    static let amber = Color(hex: 0xF39C12)
    static let red = Color(hex: 0xE74C3C)
    static let cyan = Color(hex: 0x00C0EF)
    static let purple = Color(hex: 0x9B59B6)
    static let teal = Color(hex: 0x39CCCC)

    // MARK: - LCARS Accent Colors
    static let lcarsBlue = Color(hex: 0x5B99F5)
    static let lcarsPeach = Color(hex: 0xFFAA77)
    static let lcarsPink = Color(hex: 0xFF7799)
    static let lcarsGreen = Color(hex: 0x66CC99)

    // MARK: - Semantic Colors
    static let statusUpToDate = green
    static let statusNeedsUpdate = amber
    static let statusUnreachable = red
    static let accent = cyan
}

extension Color {
    init(hex: UInt, opacity: Double = 1.0) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}
