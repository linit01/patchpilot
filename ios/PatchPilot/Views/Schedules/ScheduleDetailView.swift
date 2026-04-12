import SwiftUI

/// Detail view for a single schedule
struct ScheduleDetailView: View {
    let schedule: Schedule
    @State private var errorMessage: String?
    @Environment(\.dismiss) private var dismiss

    private let api = APIClient.shared

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    // Status
                    HStack {
                        Image(systemName: schedule.enabled ? "checkmark.circle.fill" : "pause.circle.fill")
                            .foregroundColor(schedule.enabled ? Theme.green : Theme.textMuted)
                        Text(schedule.enabled ? "Active" : "Disabled")
                            .foregroundColor(schedule.enabled ? Theme.green : Theme.textMuted)
                        Spacer()
                    }
                    .font(.headline)

                    // Details
                    VStack(spacing: 12) {
                        detailRow("Name", schedule.name)
                        detailRow("Days", schedule.daysDisplay)
                        detailRow("Time Window", schedule.timeWindowDisplay)
                        detailRow("Auto Reboot", schedule.autoReboot ? "Yes" : "No")
                        if let hostIds = schedule.hostIds {
                            detailRow("Hosts", "\(hostIds.count) host\(hostIds.count == 1 ? "" : "s")")
                        }
                    }
                    .padding()
                    .background(Theme.bgCard)
                    .cornerRadius(10)
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border, lineWidth: 1))

                    // Run Now button
                    Button(action: runNow) {
                        Label("Run Now", systemImage: "play.circle.fill")
                            .frame(maxWidth: .infinity)
                            .padding()
                            .background(Theme.cyan)
                            .foregroundColor(.white)
                            .cornerRadius(10)
                    }

                    if let error = errorMessage {
                        Text(error)
                            .font(.caption)
                            .foregroundColor(Theme.red)
                    }
                }
                .padding()
            }
            .background(Theme.bgBlack)
            .navigationTitle(schedule.name)
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }

    private func detailRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .font(.subheadline)
                .foregroundColor(Theme.textSecondary)
            Spacer()
            Text(value)
                .font(.subheadline)
                .foregroundColor(Theme.textPrimary)
        }
    }

    private func runNow() {
        Task {
            do {
                try await api.postVoid("/api/schedules/\(schedule.id)/run")
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
