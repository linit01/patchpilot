import SwiftUI

/// List of auto-patch schedules
struct ScheduleListView: View {
    @State private var schedules: [Schedule] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var selectedSchedule: Schedule?

    private let api = APIClient.shared

    var body: some View {
        List {
            if isLoading && schedules.isEmpty {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Theme.bgBlack)
            } else if schedules.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "calendar.badge.clock")
                        .font(.largeTitle)
                        .foregroundColor(Theme.textMuted)
                    Text("No schedules configured")
                        .foregroundColor(Theme.textMuted)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 40)
                .listRowBackground(Theme.bgBlack)
            } else {
                ForEach(schedules) { schedule in
                    Button {
                        selectedSchedule = schedule
                    } label: {
                        scheduleRow(schedule)
                    }
                    .listRowBackground(Theme.bgPanel)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bgBlack)
        .refreshable { await loadSchedules() }
        .task { await loadSchedules() }
        .sheet(item: $selectedSchedule) { schedule in
            ScheduleDetailView(schedule: schedule)
        }
    }

    private func scheduleRow(_ schedule: Schedule) -> some View {
        HStack(spacing: 12) {
            Image(systemName: schedule.enabled ? "calendar.circle.fill" : "calendar.circle")
                .font(.title2)
                .foregroundColor(schedule.enabled ? Theme.green : Theme.textMuted)

            VStack(alignment: .leading, spacing: 4) {
                Text(schedule.name)
                    .font(.subheadline)
                    .fontWeight(.medium)
                    .foregroundColor(Theme.textPrimary)

                Text(schedule.daysDisplay)
                    .font(.caption)
                    .foregroundColor(Theme.textSecondary)
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 4) {
                Text(schedule.timeWindowDisplay)
                    .font(.caption)
                    .foregroundColor(Theme.cyan)

                Text(schedule.enabled ? "Active" : "Disabled")
                    .font(.caption2)
                    .foregroundColor(schedule.enabled ? Theme.green : Theme.textMuted)
            }
        }
        .padding(.vertical, 4)
    }

    private func loadSchedules() async {
        isLoading = true
        do {
            schedules = try await api.get("/api/schedules")
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}

extension Schedule: Hashable {
    static func == (lhs: Schedule, rhs: Schedule) -> Bool { lhs.id == rhs.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}
