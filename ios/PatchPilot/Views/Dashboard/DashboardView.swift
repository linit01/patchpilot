import SwiftUI
import Charts

/// Main dashboard with stats cards and charts
struct DashboardView: View {
    @StateObject private var statsService = StatsService()
    @StateObject private var hostService = HostService()
    @EnvironmentObject var authService: AuthService
    @State private var isRefreshing = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    // Stats Cards
                    if let stats = statsService.stats {
                        statsGrid(stats)
                    }

                    // Patch Activity Chart
                    if let chart = statsService.chartData, !chart.patchActivity.isEmpty {
                        patchActivityChart(chart.patchActivity)
                    }

                    // OS Distribution
                    if let chart = statsService.chartData, !chart.osDistribution.isEmpty {
                        osDistributionSection(chart.osDistribution)
                    }

                    // Quick Host Summary
                    if !hostService.hosts.isEmpty {
                        hostSummarySection
                    }
                }
                .padding()
            }
            .background(Theme.bgBlack)
            .navigationTitle("Dashboard")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(action: refreshAll) {
                        Image(systemName: "arrow.clockwise")
                            .foregroundColor(Theme.cyan)
                    }
                    .disabled(isRefreshing)
                }

                if authService.currentUser?.role.canWrite == true {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button(action: checkAll) {
                            Image(systemName: "magnifyingglass")
                                .foregroundColor(Theme.cyan)
                        }
                    }
                }
            }
            .refreshable { await loadData() }
            .task { await loadData() }
        }
    }

    // MARK: - Stats Grid

    private func statsGrid(_ stats: DashboardStats) -> some View {
        LazyVGrid(columns: [
            GridItem(.flexible()),
            GridItem(.flexible())
        ], spacing: 12) {
            StatsCard(title: "Total Hosts", value: stats.totalHosts,
                      icon: "desktopcomputer", color: Theme.blue)
            StatsCard(title: "Up to Date", value: stats.upToDate,
                      icon: "checkmark.circle.fill", color: Theme.green)
            StatsCard(title: "Need Updates", value: stats.needUpdates,
                      icon: "exclamationmark.triangle.fill", color: Theme.amber)
            StatsCard(title: "Unreachable", value: stats.unreachable,
                      icon: "wifi.slash", color: Theme.red)
        }
    }

    // MARK: - Patch Activity Chart

    private func patchActivityChart(_ activity: [PatchActivity]) -> some View {
        // Flatten by_os into individual entries for stacked bars
        let osEntries: [PatchActivityOSEntry] = activity.flatMap { item in
            (item.byOs ?? [:]).map { os, count in
                PatchActivityOSEntry(day: String(item.day.suffix(5)), os: os, count: count)
            }
        }
        let failedEntries: [PatchActivityOSEntry] = activity.map { item in
            PatchActivityOSEntry(day: String(item.day.suffix(5)), os: "Failed", count: item.failed)
        }
        let allEntries = osEntries + failedEntries.filter { $0.count > 0 }

        // Collect unique OS names (excluding Failed) for legend
        let osNames = Array(Set(osEntries.map(\.os))).sorted()

        return VStack(alignment: .leading, spacing: 10) {
            Text("Patch Activity (7 days)")
                .font(.headline)
                .foregroundColor(Theme.textPrimary)

            Chart(allEntries) { entry in
                BarMark(
                    x: .value("Day", entry.day),
                    y: .value("Packages", entry.count)
                )
                .foregroundStyle(osColor(entry.os))
                .position(by: .value("Type", entry.os))
            }
            .chartYAxisLabel("Packages")
            .frame(height: 180)

            // Dynamic legend
            FlowLegend(osNames: osNames)
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Theme.border, lineWidth: 1)
        )
    }

    /// Fixed OS color map matching the web UI
    private func osColor(_ os: String) -> Color {
        switch os {
        case "Debian", "debian":   return Theme.amber
        case "Darwin", "darwin":   return Theme.blue
        case "Windows", "windows": return Theme.greenBright
        case "RedHat", "redhat":   return Theme.purple
        case "Ubuntu", "ubuntu":   return Theme.teal
        case "Failed":             return Theme.red
        default:                   return Theme.lcarsBlue
        }
    }

    // MARK: - OS Distribution

    private func osDistributionSection(_ distribution: [OSDistribution]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("OS Distribution")
                .font(.headline)
                .foregroundColor(Theme.textPrimary)

            ForEach(distribution) { item in
                HStack {
                    Text(item.os)
                        .font(.subheadline)
                        .foregroundColor(Theme.textPrimary)
                    Spacer()
                    Text("\(item.count)")
                        .font(.subheadline)
                        .fontWeight(.semibold)
                        .foregroundColor(Theme.cyan)
                }
            }
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Theme.border, lineWidth: 1)
        )
    }

    // MARK: - Host Summary

    private var hostSummarySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Hosts Needing Attention")
                    .font(.headline)
                    .foregroundColor(Theme.textPrimary)
                Spacer()
            }

            let needsAttention = hostService.hosts.filter {
                $0.status == .updatesAvailable || $0.status == .unreachable || $0.rebootRequired == true
            }.prefix(5)

            if needsAttention.isEmpty {
                Text("All hosts are up to date")
                    .font(.subheadline)
                    .foregroundColor(Theme.green)
            } else {
                ForEach(Array(needsAttention)) { host in
                    HostRow(host: host)
                    if host.id != needsAttention.last?.id {
                        Divider().background(Theme.border)
                    }
                }
            }
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Theme.border, lineWidth: 1)
        )
    }

    // MARK: - Data Loading

    private func loadData() async {
        do {
            try await statsService.fetchAll()
            try await hostService.fetchHosts()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func refreshAll() {
        Task { await loadData() }
    }

    private func checkAll() {
        Task {
            do {
                try await hostService.checkAllHosts()
                // Wait a moment then refresh
                try await Task.sleep(for: .seconds(2))
                await loadData()
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
