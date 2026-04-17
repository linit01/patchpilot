import SwiftUI

/// Detail view for a single host: status, packages, actions, history
struct HostDetailView: View {
    let host: Host
    @StateObject private var hostService = HostService()
    @StateObject private var patchService = PatchService()
    @EnvironmentObject var authService: AuthService
    @State private var packages: [Package] = []
    @State private var hostHistory: [PatchHistoryRecord] = []
    @State private var isChecking = false
    @State private var showPatchSheet = false
    @State private var errorMessage: String?
    @State private var packageError: String?
    @State private var packagesLoaded = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Host Info Card
                hostInfoCard

                // Actions (for write users)
                if authService.currentUser?.role.canWrite == true {
                    actionsSection
                }

                // Pending Packages — always shown once load completes
                if !packages.isEmpty || host.status == .updatesAvailable {
                    packagesSection
                }

                // Patch History
                if !hostHistory.isEmpty {
                    historySection
                }
            }
            .padding()
        }
        .background(Theme.bgBlack)
        .navigationTitle(host.hostname)
        .navigationBarTitleDisplayMode(.inline)
        .task { await loadDetails() }
        .sheet(isPresented: $showPatchSheet) {
            PatchConfirmSheet(
                allHosts: [host],
                onDismiss: { showPatchSheet = false }
            )
        }
    }

    // MARK: - Host Info

    private var hostInfoCard: some View {
        VStack(spacing: 12) {
            HStack {
                StatusBadge(status: host.status)
                if host.rebootRequired == true {
                    RebootBadge()
                }
                Spacer()
            }

            infoRow("IP Address", host.ipAddress ?? "—")
            infoRow("OS", [host.osFamily, host.osVersion].compactMap { $0 }.joined(separator: " "))
            infoRow("SSH User", host.sshUser ?? "—")
            infoRow("SSH Port", host.sshPort.map { "\($0)" } ?? "22")
            infoRow("Last Checked", host.lastChecked ?? "Never")
            infoRow("Pending Updates", "\(host.totalUpdates ?? 0)")
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border, lineWidth: 1))
    }

    private func infoRow(_ label: String, _ value: String) -> some View {
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

    // MARK: - Actions

    private var actionsSection: some View {
        HStack(spacing: 12) {
            Button(action: checkHost) {
                Label(isChecking ? "Checking..." : "Check Updates", systemImage: "magnifyingglass")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
            }
            .buttonStyle(.bordered)
            .tint(Theme.blue)
            .disabled(isChecking)

            if host.status == .updatesAvailable {
                Button(action: { showPatchSheet = true }) {
                    Label("Patch", systemImage: "arrow.down.circle.fill")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                }
                .buttonStyle(.bordered)
                .tint(Theme.green)
            }
        }
    }

    // MARK: - Packages

    private var packagesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Pending Updates")
                    .font(.headline)
                    .foregroundColor(Theme.textPrimary)
                Spacer()
                Text("\(packages.count) package\(packages.count == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundColor(Theme.amber)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Theme.amber.opacity(0.15))
                    .cornerRadius(8)
            }

            if !packagesLoaded {
                HStack {
                    ProgressView().scaleEffect(0.7)
                    Text("Loading packages...")
                        .font(.caption)
                        .foregroundColor(Theme.textMuted)
                }
            } else if let err = packageError {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(Theme.amber)
                    Text(err)
                        .font(.caption)
                        .foregroundColor(Theme.amber)
                }
            } else if packages.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(Theme.green)
                    Text("No pending packages found")
                        .font(.caption)
                        .foregroundColor(Theme.textMuted)
                }
            } else {
                ForEach(packages) { pkg in
                    VStack(spacing: 0) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .top, spacing: 8) {
                                // Package name + type
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(pkg.name)
                                        .font(.subheadline)
                                        .fontWeight(.medium)
                                        .foregroundColor(Theme.textPrimary)
                                    if let type = pkg.updateType {
                                        Text(type)
                                            .font(.caption2)
                                            .foregroundColor(Theme.textMuted)
                                    }
                                }

                                Spacer()

                                // Version: current → available
                                HStack(spacing: 4) {
                                    if let current = pkg.currentVersion {
                                        Text(current)
                                            .font(.caption)
                                            .foregroundColor(Theme.textMuted)
                                    }
                                    if pkg.currentVersion != nil && pkg.availableVersion != nil {
                                        Image(systemName: "arrow.right")
                                            .font(.caption2)
                                            .foregroundColor(Theme.textMuted)
                                    }
                                    if let avail = pkg.availableVersion {
                                        Text(avail)
                                            .font(.caption)
                                            .fontWeight(.semibold)
                                            .foregroundColor(Theme.green)
                                    }
                                }
                            }

                            // Exclusion ID row — shown for mas/winget when a package_id is known
                            if let pid = pkg.packageId,
                               let type = pkg.updateType,
                               (type == "mas" || type == "winget") {
                                HStack(spacing: 6) {
                                    Image(systemName: type == "mas" ? "bag.fill" : "shippingbox.fill")
                                        .font(.caption2)
                                        .foregroundColor(Theme.textMuted)
                                    Text("ID: \(pid)")
                                        .font(.system(.caption2, design: .monospaced))
                                        .foregroundColor(Theme.textSecondary)
                                    Spacer()
                                    Button {
                                        UIPasteboard.general.string = pid
                                    } label: {
                                        Image(systemName: "doc.on.doc")
                                            .font(.caption2)
                                            .foregroundColor(Theme.cyan)
                                    }
                                    .buttonStyle(.plain)
                                }
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Theme.bgCardInner)
                                .cornerRadius(6)
                            }
                        }
                        .padding(.vertical, 6)

                        Divider()
                            .background(Theme.border)
                            .opacity(pkg.id == packages.last?.id ? 0 : 1)
                    }
                }
            }
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border, lineWidth: 1))
    }

    // MARK: - History

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Patch History")
                .font(.headline)
                .foregroundColor(Theme.textPrimary)

            ForEach(hostHistory) { record in
                HStack {
                    Image(systemName: record.isSuccess ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundColor(record.isSuccess ? Theme.green : Theme.red)

                    VStack(alignment: .leading) {
                        Text(record.createdAt ?? "—")
                            .font(.caption)
                            .foregroundColor(Theme.textSecondary)
                        if let pkgs = record.packagesUpdated, pkgs > 0 {
                            Text("\(pkgs) package\(pkgs == 1 ? "" : "s") updated")
                                .font(.caption2)
                                .foregroundColor(Theme.textMuted)
                        }
                    }

                    Spacer()

                    Text(record.durationDisplay)
                        .font(.caption)
                        .foregroundColor(Theme.textMuted)
                }
                .padding(.vertical, 2)
            }
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border, lineWidth: 1))
    }

    // MARK: - Data Loading

    private func loadDetails() async {
        // Load independently so a history failure doesn't block packages
        async let pkgTask: [Package] = hostService.fetchPackages(hostname: host.hostname)
        async let historyTask: [PatchHistoryRecord] = patchService.fetchHostHistory(hostId: host.id)

        do {
            packages = try await pkgTask
        } catch {
            packageError = error.localizedDescription
        }
        packagesLoaded = true

        do {
            hostHistory = try await historyTask
        } catch {
            // History failure is non-critical, suppress
        }
    }

    private func checkHost() {
        isChecking = true
        Task {
            do {
                try await hostService.checkHost(hostname: host.hostname)
                try await Task.sleep(for: .seconds(2))
                await loadDetails()
            } catch {
                errorMessage = error.localizedDescription
            }
            isChecking = false
        }
    }
}
