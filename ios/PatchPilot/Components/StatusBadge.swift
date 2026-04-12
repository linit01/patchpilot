import SwiftUI

/// Colored status indicator pill matching PatchPilot web UI
struct StatusBadge: View {
    let status: HostStatus

    var body: some View {
        Text(status.displayName)
            .font(.caption2)
            .fontWeight(.semibold)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(backgroundColor.opacity(0.15))
            .foregroundColor(backgroundColor)
            .clipShape(Capsule())
    }

    private var backgroundColor: Color {
        switch status {
        case .upToDate: return Theme.green
        case .updatesAvailable: return Theme.amber
        case .unreachable: return Theme.red
        case .pending: return Theme.textMuted
        case .checking: return Theme.cyan
        }
    }
}

/// Reboot required indicator
struct RebootBadge: View {
    var body: some View {
        Label("Reboot", systemImage: "arrow.clockwise.circle.fill")
            .font(.caption2)
            .fontWeight(.semibold)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Theme.amber.opacity(0.15))
            .foregroundColor(Theme.amber)
            .clipShape(Capsule())
    }
}
