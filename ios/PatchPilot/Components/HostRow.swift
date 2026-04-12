import SwiftUI

/// Row displayed in the host list
struct HostRow: View {
    let host: Host

    var body: some View {
        HStack(spacing: 12) {
            // OS icon
            Image(systemName: osIcon)
                .font(.title2)
                .foregroundColor(Theme.cyan)
                .frame(width: 36)

            VStack(alignment: .leading, spacing: 4) {
                Text(host.hostname)
                    .font(.subheadline)
                    .fontWeight(.semibold)
                    .foregroundColor(Theme.textPrimary)

                HStack(spacing: 6) {
                    if let ip = host.ipAddress {
                        Text(ip)
                            .font(.caption)
                            .foregroundColor(Theme.textSecondary)
                    }
                    if let os = host.osFamily {
                        Text(os)
                            .font(.caption)
                            .foregroundColor(Theme.textMuted)
                    }
                }
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 4) {
                StatusBadge(status: host.status)

                if let updates = host.totalUpdates, updates > 0 {
                    Text("\(updates) update\(updates == 1 ? "" : "s")")
                        .font(.caption2)
                        .foregroundColor(Theme.amber)
                }

                if host.rebootRequired == true {
                    RebootBadge()
                }
            }
        }
        .padding(.vertical, 4)
    }

    private var osIcon: String {
        guard let os = host.osFamily?.lowercased() else { return "desktopcomputer" }
        if os.contains("darwin") || os.contains("macos") { return "laptopcomputer" }
        if os.contains("windows") { return "pc" }
        if os.contains("debian") || os.contains("ubuntu") { return "server.rack" }
        if os.contains("redhat") || os.contains("centos") || os.contains("rhel") { return "server.rack" }
        return "desktopcomputer"
    }
}
